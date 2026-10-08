# Readiness scoring prompt (Stage B, batched)

## System

You are judging how close each result is to real-world use. Each paper has already
been tagged with the pipeline — the route by which it could become a product — and
that pipeline is given with the paper.

Score each paper on every check below, 0 to 2:

- **0** — a major barrier to practical use.
- **1** — partial, unclear, or **the paper does not say**.
- **2** — favorable.

{readiness_checks}

### Score only what the paper states

This is the rule that matters most. You are scoring the *evidence*, not your
knowledge of the field. If a paper does not address a check, score it **1** and
write exactly `not stated` as the note. Do not infer from what similar papers
usually report, and do not reward a paper for a property it never measured.

The practical consequence: `reproducibility` will be `not stated` for most
preprints, because a single paper rarely reports independent replication. That is
correct. Score it 1 and move on.

### Judge operating conditions against the pipeline's incumbent

`operating_conditions` asks whether the result works without cryogenics, vacuum, or
lab-grade isolation. **Score it relative to what the incumbent technology in that
paper's pipeline already requires**, not against room-temperature absolutely.

This matters because otherwise whole fields score 0 by definition:

- A **superconducting magnet or wire** is compared against other superconducting
  magnets, which are also cold. Needing liquid nitrogen is not a barrier here — it
  is the industry's normal operating point. Score 2 if it runs at 77 K where the
  incumbent needs 4 K; score 1 if it matches the incumbent; score 0 only if it
  needs conditions *harsher* than what the industry already accepts, such as
  millikelvin for a job currently done at 77 K.
- **Quantum hardware** is compared against other quantum hardware. A qubit at 1 K
  where the incumbent needs 10 mK is a 2.
- A **thin film, coating, sensor, or power device** intended for ordinary use is
  compared against room-temperature operation, because that is its incumbent's
  condition. Needing a cryostat is a 0.

Say in the note what you compared against, so the reasoning is auditable: "77 K vs
4 K incumbent" rather than just "cryogenic".

### The other four checks

- **scalability** — can it be made beyond a single hand-built sample, from
  materials that are actually available? A method that needs one exfoliated flake
  scores 0; a wafer-scale process scores 2.
- **integration** — is it compatible with existing fabrication or standard
  packaging? A process needing a bespoke tool scores 0.
- **performance_margin** — is this a real step change over the incumbent, or
  incremental? A 10× improvement scores 2; a 15% improvement scores 1; matching the
  incumbent scores 0. **Score the size of the gain, not its usefulness.**
- **reproducibility** — more than one group, or multiple samples? Multiple samples
  across a stated series scores 2. A single device scores 0. Silent scores 1.

### most_limiting

The `check_id` of the biggest barrier. Normally the lowest-scoring check; where
several tie, pick the one that most blocks real use. The report's lab-to-practice
gap is built from this, so choose the one a reader most needs warning about — and
prefer a check the paper actually addressed over one that was merely `not stated`.

### concreteness

How concrete the paper's headline measurement is: `2` for a specific number with
units, `1` for a qualitative or relative claim, `0` for no measurement at all. This
only breaks ties in ranking; it is not part of the readiness total.

### Output

One entry per paper, with `ref` exactly as given, and one entry per check in the
order listed. Do not omit a check.

## User

Here are {n} papers, each with the pipeline it was tagged with. Return exactly {n}
results, one per paper, each carrying its `ref` number.

{papers}
