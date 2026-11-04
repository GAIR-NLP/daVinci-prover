import Mathlib.Data.Real.Basic

/-
Let $f(x)$ be an even function and $\varphi(x)$ be an odd function. Then the composition $\varphi[\varphi(x)]$ is an odd function.
-/

open Function

variable (φ : ℝ → ℝ)

/-- A function φ is odd if φ(-x) = -φ(x) for all x. -/
def OddFunction (φ : ℝ → ℝ) := ∀ x, φ (-x) = -φ (x)

/-- A function f is even if f(-x) = f(x) for all x. -/
def EvenFunction (f : ℝ → ℝ) := ∀ x, f (-x) = f (x)


/-- If φ is an odd function, then φ[φ(x)] is also an odd function. -/
theorem CompositionOfOddFunctions (h : OddFunction φ) : OddFunction (φ ∘ φ) := by
  sorry
