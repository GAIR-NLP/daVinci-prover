import Lean

/- One-shot draft elaboration. The candidate is read unchanged so diagnostic
   positions and local goals refer to its original source. This executable is
   not a final-proof verifier: explicit sorry/admit holes remain assumptions. -/
open Lean Elab

namespace DvpLocalDraft

def positionJson (pos : Position) : Json :=
  Json.mkObj [("line", toJson pos.line), ("column", toJson pos.column)]

def holeJson (ctx : ContextInfo) (stx : Syntax) (goal : String) : Json :=
  let start := stx.getPos?.getD 0
  Json.mkObj [
    ("pos", positionJson (ctx.fileMap.toPosition start)),
    ("endPos", positionJson (ctx.fileMap.toPosition (stx.getTailPos?.getD start))),
    ("goal", toJson goal)]

partial def collectHoles (tree : InfoTree) (outer : Option ContextInfo := none) :
    IO (Array Json) := do
  match tree with
  | .context ctx tree => collectHoles tree (ctx.mergeIntoOuter? outer)
  | .node info children =>
    let mut result := #[]
    if let some ctx := outer then
      match info with
      | .ofTacticInfo ti =>
        if ti.stx.getKind == ``Lean.Parser.Tactic.tacticSorry && !ti.goalsBefore.isEmpty then
          let ctx := { ctx with mctx := ti.mctxBefore }
          let goal ← ctx.ppGoals [ti.goalsBefore.head!]
          -- Do not also count the term created by elaborating this tactic.
          return #[holeJson ctx ti.stx goal.pretty]
      | .ofTermInfo ti =>
        if ti.stx.getKind == ``Lean.Parser.Term.sorry then
          if let some expected := ti.expectedType? then
            let goal ← ctx.runMetaM ti.lctx do
              let metavariable ← Meta.mkFreshExprMVar expected
              Meta.ppGoal metavariable.mvarId!
            result := result.push (holeJson ctx ti.stx goal.pretty)
      | _ => pure ()
    for child in children do
      result := result ++ (← collectHoles child outer)
    return result
  | _ => return #[]

def messageJson (message : Message) : IO Json := do
  let severity := match message.severity with
    | .information => "info"
    | .warning => "warning"
    | .error => "error"
  return Json.mkObj [
    ("pos", positionJson message.pos),
    ("endPos", toJson (message.endPos.map positionJson)),
    ("severity", toJson severity),
    ("data", toJson (← message.data.toString))]

unsafe def elaborateFile (file : String) : IO Json := do
  let code ← IO.FS.readFile file
  initSearchPath (← findSysroot)
  enableInitializersExecution
  let inputCtx := Parser.mkInputContext code file
  let (header, parserState, messages) ← Parser.parseHeader inputCtx
  let (env, messages) ← processHeader header {} messages inputCtx
  let commandState := { Command.mkState env messages {} with infoState.enabled := true }
  let result ← IO.processCommands inputCtx parserState commandState
  let state := result.commandState
  let messages ← state.messages.toList.mapM messageJson
  let mut holes := #[]
  for tree in state.infoState.trees do
    holes := holes ++ (← collectHoles tree)
  return Json.mkObj [("messages", toJson messages), ("sorries", toJson holes)]

end DvpLocalDraft

unsafe def main (args : List String) : IO Unit := do
  let [file] := args
    | throw (IO.userError "Usage: lean --run LocalDraftVerifier.lean CANDIDATE.lean")
  let result ← DvpLocalDraft.elaborateFile file
  -- A fresh final line distinguishes the result from candidate #eval output.
  IO.println ("\nDVP_LOCAL_DRAFT_JSON:" ++ result.compress)
