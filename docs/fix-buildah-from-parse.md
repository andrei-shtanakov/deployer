# When Buildah checks FROM (local FROM row, design §6.3)

This note records pinned source for design §6.3
(`docs/superpowers/specs/2026-09-25-ci-fix-authoring-design.md`). It covers the local
FROM template, which was still a hypothesis when this note was written. The row then
read "a build-stage step exists and no parse error" as proof that the diagnosed FROM argument-count error is gone. That holds
only if the builder checks every instruction before it builds any stage. §6.3 says this
is established for BuildKit. For Podman it has to be settled from Buildah's parse path at
the pinned version and from a recording with a bad FROM in a later stage. The source was
read on 2026-09-25. It was read only; nothing was built or run for this note.

**Verdict: no.** Buildah parses the whole file into nodes and stages before it builds
anything, but that pass does not check how many arguments FROM has. The
`FROM requires either one argument, or three` check runs **per stage**, when that stage
starts to build. A stage that nothing depends on is skipped by default, so its FROM is
never checked. The file-wide rule is therefore **refuted** for Podman. On 2026-09-25 the
owner enabled the local FROM row on a narrower rule instead: the corrected stage's own
FROM step line, which step 6 below backs (details under "What this means for the local
FROM row" and its addendum).

## Pins

- Repository: `github.com/podman-container-tools/buildah` (formerly
  `containers/buildah`; see `docs/fix-stage-name-grammar.md`), tag `v1.42.0`
  (2025-10-17), commit `1ba41f035a103e8cf1132c7399bf0ed75d6df53e`.
  - `go.mod` line 30: `github.com/openshift/imagebuilder v1.2.19`.
- Repository: `github.com/openshift/imagebuilder`, tag `v1.2.19`, commit
  `812868a1428365cb2418a5ea851e59c699f4c6ab`.
- Podman: `github.com/containers/podman`, tag `v5.7.0`, commit
  `0370128fc8dcae93533334324ef838db8f8da8cb`, `go.mod` line 14:
  `github.com/containers/buildah v1.42.0`.

**Why v1.42.0 and not v1.45.1.** `docs/fix-stage-name-grammar.md` pins Buildah v1.45.1,
the latest release, because it states a grammar rule for builders in general. It then
checks that the rule also holds at v1.42.0. This note explains recorded behaviour, so it
pins the Buildah that made the recordings: Podman 5.7.0 with Buildah 1.42.0 (the
`environment.json` of every L recording). The claims below are about v1.42.0 only.

## The path

### 1. Whole-file parse: syntax only

`imagebuildah/build.go` line 414 (`buildDockerfilesOnce`):

```go
	mainNode, err := imagebuilder.ParseDockerfile(bytes.NewReader(dockerfilecontents[0]))
```

imagebuilder `evaluator.go` lines 14–20: `ParseDockerfile` only calls the BuildKit
low-level `parser.Parse(r)` and returns `result.AST`. It builds the line tree and does not
check any instruction's arguments.

### 2. Whole-file stage split: counts only an empty FROM

`imagebuildah/build.go` lines 466 and 477:

```go
	stages, err := imagebuilder.NewStages(mainNode, b)
	...
	return exec.Build(ctx, stages)
```

imagebuilder `builder.go` lines 292–383 (`NewStages`) runs over every stage before any of
them is built (line 350: `for i, root := range SplitBy(node, command.From) {`). The only
FROM checks in it are in `getStageFrom`, lines 298–303:

```go
			if child.Next == nil {
				return "", "", errors.New("FROM requires an argument")
			}
			if child.Next.Value == "" {
				return "", "", errors.New("FROM requires a non-empty argument")
			}
```

So a FROM with **no** argument fails for the whole file before any stage runs. A FROM
with two arguments, or four or more, passes this step. `extractNameFromNode` (lines
435–448) returns `false` for `FROM python:3.12-slim extra`, and the stage gets its
position as its name.

### 3. Pre-pass in `Executor.Build`: reads only the image

`imagebuildah/executor.go` lines 790–918 walk every line of every stage before any stage
is built, to find which stages depend on which. For FROM the pre-pass reads only the
second token (line 796: `case "FROM":`, line 797: `if child.Next != nil { // second token
on this line`). It never counts the arguments. The same is true of `newExecutor` (lines
372–394), which only notes the first FROM for global ARGs (line 390: `case "FROM":`,
line 391: `foundFirstStage = true`).

### 4. Stages that nothing depends on are skipped

`imagebuildah/executor.go` lines 973–983, inside each stage's goroutine, before
`buildStage` is called (line 986):

```go
				// Skip stage if it is not needed by TargetStage
				// or any of its dependency stages and `SkipUnusedStages`
				// is not set to `false`.
				if stageDependencyInfo, ok := dependencyMap[stages[index].Name]; ok {
					if !stageDependencyInfo.NeededByTarget && b.skipUnusedStages != types.OptionalBoolFalse {
						logrus.Debugf("Skipping stage with Name %q and index %d since its not needed by the target stage", stages[index].Name, index)
```

`pkg/cli/common.go` line 313 (`GetBudFlags`, which Podman uses: see
`cmd/podman/common/build.go` line 74 at `v5.7.0`) makes this the default:
`fs.BoolVar(&flags.SkipUnusedStages, "skip-unused-stages", true, ...)`. Line 255 of the
same file sets `--jobs` to 1 by default. `imagebuildah/build.go` lines 178–188 then give
a `semaphore.NewWeighted(1)`, so stages start one at a time, in order.

### 5. The argument-count check runs when the stage starts

`imagebuildah/executor.go` lines 523–530 (`buildStage`):

```go
	stage := stages[stageIndex]
	ib := stage.Builder
	node := stage.Node
	base, err := ib.From(node)
	if err != nil {
		logrus.Debugf("buildStage(node.Children=%#v)", node.Children)
		return "", nil, false, err
	}
```

imagebuilder `builder.go` lines 655–677 (`Builder.From`) sends the stage's FROM through
`b.Run(step, NoopExecutor, false)`. The `evaluateTable` (line 750) maps
`command.From: from` (line 756). In `dispatchers.go` lines 320–327, `from` holds the
check:

```go
	switch {
	case len(args) == 1:
	case len(args) == 3 && len(args[0]) > 0 && strings.EqualFold(args[1], "as") && len(args[2]) > 0:

	default:
		return fmt.Errorf("FROM requires either one argument, or three: FROM <source> [AS <name>]")
	}
```

This is the only place in either repository with that error text. Buildah's copy is the
vendored `vendor/github.com/openshift/imagebuilder/dispatchers.go:326`.
`buildStage` returns the error unwrapped. `Executor.Build` then ends the build on the
first stage error (lines 1019–1022: `if r.Error != nil { ... return "", nil, r.Error }`).

### 6. Step lines are printed only after the check passes

The stage's logger is set up in `buildStage` **after** `ib.From` (executor.go lines
586–606). The stage's first line, its `FROM`, is printed by `StageExecutor.prepare`
(`stage_executor.go` line 965: `s.log("FROM %s", displayFrom)`). So a stage whose FROM
fails the check prints no step line.

The `[i/n] ` prefix seen in recording `l7` comes from executor.go lines 590–593:

```go
			prefix := b.logPrefix
			if len(stages) > 1 {
				prefix += fmt.Sprintf("[%d/%d] ", stageIndex+1, len(stages))
			}
```

`n` counts every stage in the file, including skipped ones. Recording `l5` shows only
`[2/2]` lines for this reason. `STEP k/m` counts `len(stage.Node.Children)+1` (lines
595–600).

## Cross-check: recording L4

`tests/fixtures/recordings/local/l4-from-bad-later/` (PR #97, branch
`feat/fix-6-l-recordings`, commit `f665256c88d1145afc2814fd16d3c182d120277f`;
Podman 5.7.0 / Buildah 1.42.0):

```dockerfile
FROM python:3.12-slim AS a
RUN true
FROM python:3.12-slim extra
RUN true
```

It exited `125`. stdout was empty, with no `STEP` line at all. stderr was
`Error: FROM requires either one argument, or three: FROM <source> [AS <name>]`. The
`Error: ` prefix and the exit code are added by Podman's CLI, whose code was not read for
this note.

The recording **agrees with the source, but it cannot tell the two readings apart.**
Stage `a` is not needed by the final stage, so step 4 skips it. The final stage then
fails in step 5 before it prints anything. A builder that checks every FROM before any
stage would give the same output. L4 therefore does **not** show that Buildah checks the
whole file first. The source shows that it does not.

Two more recordings fit the same source:

- `l5-stages-same-image`: stage `a` has no dependents, so step 4 skips it. stdout has
  only `[2/2]` lines.
- `l7-stages-both-built`: `COPY --from=a` makes stage `a` needed, so both stages build,
  one after the other, with `[1/2]` then `[2/2]` lines.

Two later recordings confirm both predictions this note made from the source alone:

- `l8-from-bad-after-built-stage`: the L4 Dockerfile with `COPY --from=a …` in the
  second stage. It prints stage `a`'s `[1/2] STEP 1/2: FROM python:3.12-slim AS a` and
  `[1/2] STEP 2/2: RUN true`, then fails with the same error (exit 125). The FROM check
  runs per stage, when the stage starts (step 5).
- `l9-from-bad-in-skipped-stage`: the bad FROM sits in a stage nothing depends on. That
  stage is skipped (step 4) and never checked, and the build **exits 0** with the defect
  still in the file.

## What this means for the local FROM row

For BuildKit, §6.3 says the instruction parse comes before building stages. For Podman
(Buildah v1.42.0), the argument-count error in `FROM <ref> <token>` is found at
**stage start**, not at parse time. So:

1. A build-stage step line and no parse error do **not** show that a later stage's FROM
   passes the argument check. An earlier stage can print its steps before a later FROM
   fails.
2. Even a **successful** local build (exit 0) does not show it, if the corrected FROM is
   in a stage that nothing depends on. That stage is skipped by default and its FROM is
   never checked.
3. The only local evidence the source supports is narrower. The stage that holds the
   corrected FROM must have started, which shows as its own `FROM` step line. Checking
   that needs the step line to be bound to that stage, which §6.3 says the FROM row does
   not need. It also fails on its own terms when the stage is skipped. That would change
   the design, and this note does not make that change.

The file-wide premise §6.3 rested on is **refuted** for the pinned Podman (`l8`, `l9`;
the L4 recording alone cannot settle it, because stage `a` was skipped).

**Addendum (2026-09-25, owner decision).** The local FROM row is **enabled** on the
narrower rule of point 3: the evidence is the corrected stage's own FROM step line,
`STEP 1/m: <corrected FROM>` (with or without the `[i/n] ` prefix), exactly once, with
no parse error and no error bound to that step. Step 6 backs it: the line is printed
only after that stage's FROM check passed. A skipped stage prints no line, so it is
never confirmed. One more source fact bounds the rule: `StageExecutor.prepare`
(`stage_executor.go` lines 949–965) prints `displayFrom`, the base **after** ARG/env
expansion with quotes removed, `--platform=` in front and ` AS <name>` only for a
non-numeric name. Two FROMs written differently can therefore print the same line, so
the matcher refuses (`binding ambiguous`) whenever any FROM in the file holds `$`, a
quote or a backslash, or has a numeric stage name, and compares FROMs by that rebuilt
form (design §6.3).
