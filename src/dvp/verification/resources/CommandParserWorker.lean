import Lean

/- A syntax-only JSONL service. Candidate commands are never elaborated. -/
open Lean

namespace DvpCommandParser

partial def nameParts : Name → Array String
  | .anonymous => #[]
  | .str p s => (nameParts p).push s
  | .num p n => (nameParts p).push (toString n)

def isDocumentation (stx : Syntax) : Bool :=
  stx.isOfKind ``Parser.Command.docComment ||
  stx.isOfKind ``Parser.Command.moduleDoc

partial def normalized (stx : Syntax) : Json :=
  match stx with
  | .missing => Json.arr #[toJson "missing"]
  | .atom _ value => Json.arr #[toJson "atom", toJson value]
  | .ident _ _ value _ => Json.arr #[toJson "ident", toJson (nameParts value)]
  | .node _ kind children =>
    let children := children.filter (fun child => !isDocumentation child)
    Json.arr #[toJson "node", toJson kind.toString, Json.arr (children.map normalized)]

def isValue (stx : Syntax) : Bool :=
  stx.isOfKind ``Parser.Command.declValSimple ||
  stx.isOfKind ``Parser.Command.declValEqns ||
  stx.isOfKind ``Parser.Command.whereStructInst

def normalizedSignature (stx : Syntax) : Json :=
  -- Only the outer declaration's value is replaceable. A command quotation
  -- inside the signature can itself contain declaration values; preserve them.
  if stx.isOfKind ``Parser.Command.declaration then
    normalized <| stx.modifyArg 1 fun payload =>
      payload.modifyArgs fun children => children.map fun child =>
        if isValue child then mkNode `DvpCommandParser.declarationValue #[] else child
  else normalized stx

partial def findNode (predicate : Syntax → Bool) (stx : Syntax) : Option Syntax :=
  if predicate stx then some stx
  else stx.getArgs.findSome? (findNode predicate)

def optionalPosition (stx : Option Syntax) (tail := false) : Json :=
  match stx with
  | none => Json.null
  | some stx =>
    let position := if tail then stx.getTailPos? else stx.getPos?
    match position with
    | none => Json.null
    | some position => toJson position.byteIdx

def declarationPayload (stx : Syntax) : Option Syntax :=
  if stx.isOfKind ``Parser.Command.declaration then some stx[1]
  else none

def canonicalCommand (stx : Syntax) : Syntax :=
  -- Mathlib's trusted lemma parser is exactly the theorem parser under this
  -- wrapper; mirror its documented macro without running candidate macros.
  if stx.getKind == `lemma || stx.getKind.toString.endsWith ".lemma" then
    let stx := stx.modifyArg 1 fun body =>
      (body.modifyArg 0 (mkAtomFrom · "theorem")).setKind ``Parser.Command.theorem
    stx.setKind ``Parser.Command.declaration
  else stx

partial def declarationId (stx : Syntax) : Option Syntax :=
  if stx.isOfKind ``Parser.Command.declId then some stx
  else if isValue stx || stx.isOfKind ``Parser.Command.declSig ||
      stx.isOfKind ``Parser.Command.optDeclSig then none
  else stx.getArgs.findSome? declarationId

def commandJson (stx : Syntax) : Json := Id.run do
  let canonical := canonicalCommand stx
  let decl := declarationPayload canonical
  let declId := decl.bind declarationId
  let identifier := declId.bind (findNode Syntax.isIdent)
  let value := decl.bind (fun d => d.getArgs.find? isValue)
  let body := value.bind fun v =>
    if v.isOfKind ``Parser.Command.declValSimple then some v[1]
    else some v
  let kind := if stx.getKind == `lemma || stx.getKind.toString.endsWith ".lemma" then some "lemma"
    else decl.map (fun d => d.getKind.toString)
  return Json.mkObj [
    ("start_byte", optionalPosition (some stx)),
    ("end_byte", optionalPosition (some stx) true),
    ("kind", toJson stx.getKind.toString),
    ("ast", normalized canonical),
    ("decl_kind", toJson kind),
    ("decl_name_parts", toJson (identifier.map (fun d => nameParts d.getId))),
    ("signature_start_byte", if decl.isSome then optionalPosition (some stx) else Json.null),
    ("signature_end_byte", optionalPosition value),
    ("value_start_byte", optionalPosition value),
    ("value_end_byte", optionalPosition value true),
    ("body_start_byte", optionalPosition body),
    ("body_end_byte", optionalPosition body true),
    ("signature_ast", if decl.isSome then normalizedSignature canonical else Json.null)]

partial def imports (stx : Syntax) : Array Syntax :=
  if stx.isOfKind ``Parser.Module.import then #[stx]
  else stx.getArgs.foldl (fun result child => result ++ imports child) #[]

def isNotation (stx : Syntax) : Bool :=
  stx.getKind == `Lean.Parser.Command.notation ||
  stx.getKind == `Lean.Parser.Command.mixfix

def isScopeOpen (stx : Syntax) : Bool :=
  stx.isOfKind ``Parser.Command.open

def registerNotation (state : Elab.Command.State) (input : Parser.InputContext)
    (stx : Syntax) : IO Elab.Command.State := do
  let context : Elab.Command.Context := {
    fileName := input.fileName, fileMap := input.fileMap,
    cmdPos := stx.getPos?.getD 0, snap? := none, cancelTk? := none }
  let action := Elab.Command.elabCommand stx
  match ← EIO.toIO' <| (action context).run state with
  | .error error => throw (IO.userError (← error.toMessageData.toString))
  | .ok (_, state) => return state

/-- Update only parser scope state, using fixed Lean implementations. In
particular, never dispatch a candidate declaration to a command elaborator.
Unknown namespaces can be created by source declarations we deliberately do
not elaborate; ignore their absent parser entries and let real Lean validation
report any semantic name errors. Other names in the same `open` still apply. -/
def openParserNamespaces (names : Array Ident) (scopedOnly : Bool)
    (excluded : List Name := []) : Elab.Command.CommandElabM Unit := do
  for identifier in names do
    let scope ← Elab.Command.getScope
    let resolved := ResolveName.resolveNamespace (← getEnv) scope.currNamespace
      scope.openDecls identifier.getId
    for name in resolved do
      activateScoped name
      unless scopedOnly do
        Elab.Command.modifyScope fun scope =>
          { scope with openDecls := .simple name excluded :: scope.openDecls }

def updateParserScope (stx : Syntax) : Elab.Command.CommandElabM Unit := do
  if stx.isOfKind ``Parser.Command.namespace then
    Elab.Command.elabNamespace stx
  else if stx.isOfKind ``Parser.Command.section then
    Elab.Command.elabSection stx
  else if stx.isOfKind ``Parser.Command.end then
    Elab.Command.elabEnd stx
  else if stx.isOfKind ``Parser.Command.open then
    match stx with
    | `(open $names*) => openParserNamespaces names false
    | `(open scoped $names*) => openParserNamespaces names true
    | `(open $name hiding $ids*) =>
      openParserNamespaces #[name] false (ids.toList.map (·.getId))
    -- Selective/renamed opens do not activate scoped syntax in Lean. Their
    -- declaration resolution belongs to the subsequent real Lean check.
    | _ => pure ()

def applyParserScope (state : Elab.Command.State) (input : Parser.InputContext)
    (stx : Syntax) : IO Elab.Command.State := do
  let context : Elab.Command.Context := {
    fileName := input.fileName, fileMap := input.fileMap,
    cmdPos := stx.getPos?.getD 0, snap? := none, cancelTk? := none }
  match ← EIO.toIO' <| (updateParserScope stx context).run state with
  | .error error => throw (IO.userError (← error.toMessageData.toString))
  | .ok (_, state) => return state

partial def parseCommands (initialState : Elab.Command.State) (code fileName : String)
    (trustedSource := false) (syntaxOnly := false) : IO (Json × Elab.Command.State) := do
  -- Match Lean's normal CRLF handling. The Python decoder maps positions in
  -- this normalized UTF-8 input back to the original, unmodified source text.
  let input := Parser.mkInputContext code fileName (normalizeLineEndings := true)
  let (header, initial, initialMessages) ← Parser.parseHeader input
  let mut state := initial
  let mut messages := initialMessages
  let mut commandState := initialState
  let mut commands := (imports header).map commandJson
  repeat
    let scope := commandState.scopes.head!
    let (stx, next, nextMessages) := Parser.parseCommand input
      { env := commandState.env, options := scope.opts,
        currNamespace := scope.currNamespace, openDecls := scope.openDecls } state messages
    state := next
    messages := nextMessages
    if stx.isOfKind ``Parser.Command.eoi then break
    if !isDocumentation stx then commands := commands.push (commandJson stx)
    if syntaxOnly && !isNotation stx && !isScopeOpen stx && !isDocumentation stx then
      throw (IO.userError "Trusted syntax payload contains a non-notation/open command")
    if trustedSource && isNotation stx && !messages.hasErrors then
      commandState ← registerNotation commandState input stx
    if (!syntaxOnly || isScopeOpen stx) && !messages.hasErrors then
      commandState ← applyParserScope commandState input stx
    if Parser.isTerminalCommand stx then break
  let errors ← (messages.toList ++ commandState.messages.toList).toArray.mapM fun message => message.toString
  return (Json.mkObj [("success", toJson errors.isEmpty),
    ("commands", Json.arr commands), ("errors", toJson errors)], commandState)

unsafe def serve : IO Unit := do
  initSearchPath (← findSysroot)
  enableInitializersExecution
  -- Load the fixed complete grammar used by these Mathlib benchmark files.
  -- Candidate imports are parsed as syntax and never imported here.
  let env ← importModules #[{ module := `Mathlib }] {} (loadExts := true) (leakEnv := true)
  let options := Options.empty.setBool `quotPrecheck false
  let baseState := Elab.Command.mkState env {} options
  let input ← IO.getStdin
  let output ← IO.getStdout
  output.putStrLn <| (Json.mkObj [("ready", toJson true), ("protocol", toJson (1 : Nat))]).compress
  output.flush
  repeat
    let line ← input.getLine
    if line.isEmpty then break
    let result ← match Json.parse line with
      | .error error => pure <| Json.mkObj [("success", toJson false), ("errors", toJson #[error])]
      | .ok request =>
        match request.getObjValAs? String "source" with
        | .error error => pure <| Json.mkObj [("success", toJson false), ("errors", toJson #[error])]
        | .ok source =>
          try
            let trustedSyntax := (request.getObjValAs? (Array String) "trusted_syntax").toOption.getD #[]
            let trustedSource := (request.getObjValAs? Bool "trusted_source").toOption.getD false
            let mut commandState := baseState
            for syntaxSource in trustedSyntax do
              let (result, next) ← parseCommands commandState syntaxSource "<trusted-syntax>" true true
              if (result.getObjValAs? Bool "success").toOption != some true then
                throw (IO.userError result.compress)
              commandState := next
            let (result, _) ← parseCommands commandState source "<submission>" trustedSource
            pure result
          catch error => pure <| Json.mkObj [("success", toJson false), ("errors", toJson #[error.toString])]
    output.putStrLn result.compress
    output.flush

end DvpCommandParser

unsafe def main : IO Unit := DvpCommandParser.serve
