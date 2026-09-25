# Stage-name grammar for `deployer fix` (F1)

Pinned-source note for design §4.3 (`docs/superpowers/specs/2026-09-25-ci-fix-authoring-design.md`).
F1 turns `FROM <ref> <token>` into `FROM <ref> AS <token>`, so `<token>` becomes a
stage name. It is proposed only when **both** builders R and CI use accept `<token>` as a
stage name and treat it the same way. This note records what each builder's source does,
read on 2026-09-25 (read only; nothing built or run), and the intersection that
`deployer.fix.fromfix.STAGE_NAME_RE` implements.

## BuildKit (CI: `docker buildx` / BuildKit frontend)

- Repository: `github.com/moby/buildkit`, tag `v0.33.0` (latest release, 2026-09-02),
  commit `dddd5621af04ea57823085c93a063383f71d3173`.
- File: `frontend/dockerfile/instructions/parse.go`.

Lines 420–433:

```go
var validStageName = regexp.MustCompile("^[a-z][a-z0-9-_.]*$")

func parseBuildStageName(args []string) (stageName string, err error) {
	switch {
	case len(args) == 3 && strings.EqualFold(args[1], "as"):
		stageName = strings.ToLower(args[2])
		if !validStageName.MatchString(stageName) {
			return "", errors.Errorf("invalid name for build stage: %q, name can't start with a number or contain symbols", args[2])
		}
	case len(args) != 1:
		return "", errors.New("FROM requires either one or three arguments")
	}

	return stageName, nil
}
```

Line 396 (`parseFrom`): `stageName, err := parseBuildStageName(req.args)`.

Rule: `AS` matched in any case (`strings.EqualFold`); the name is **lowercased first**
(`strings.ToLower`, Unicode-aware), then must match `^[a-z][a-z0-9-_.]*$` (Go RE2 with
Perl syntax: the class is `a-z`, `0-9`, `-`, `_`, `.`; `$` is end of text). So `Builder`
is accepted and becomes the stage `builder`; lines 852–857 (`isLowerCaseStageName`) only
raise the `StageNameCasing` lint for it.

## Buildah (local: Podman)

Buildah's `imagebuildah` does **not** use BuildKit's `instructions` package: it builds
stages with `github.com/openshift/imagebuilder`.

- Repository: `github.com/podman-container-tools/buildah` (formerly
  `containers/buildah`), tag `v1.45.1` (latest release, 2026-09-15), commit
  `c8ac1936bb2e956c1d8790eba1bb3829a1241369`.
  - `go.mod` line 30: `github.com/openshift/imagebuilder v1.2.21` (line 18 pulls
    `github.com/moby/buildkit v0.31.2`, used only for its low-level `parser`).
  - `imagebuildah/build.go` line 428:
    `mainNode, err := imagebuilder.ParseDockerfile(bytes.NewReader(dockerfilecontents[0]))`;
    line 480: `stages, err := imagebuilder.NewStages(mainNode, b)`.
  - `imagebuildah/executor.go` lines 501–508 (`stageIndexUnlocked`) look a stage up by
    exact string: `if otherStage.Name == nameOrIndex || strconv.Itoa(otherStage.Position) == nameOrIndex {`.
- Repository: `github.com/openshift/imagebuilder`, tag `v1.2.21`, commit
  `18f0d7881df1e4b71772353e86384ed2d9a12c24`, file `builder.go`.

Lines 451–464 (`extractNameFromNode`):

```go
	n = n.Next
	if !strings.EqualFold(n.Value, "as") || n.Next == nil || len(n.Next.Value) == 0 {
		return "", false
	}
	return n.Next.Value, true
```

Lines 367–378 (`NewStages`):

```go
		name, hasName := extractNameFromNode(root.Children[0])
		if !hasName {
			name = strconv.Itoa(i)
		}
		...
		processedName, err := ProcessWord(name, userArgs)
		...
		stage := Stage{
			Position: i,
			Name:     processedName,
```

Lines 214–219 (`Stages.ByName`): `if stage.Name == name {` — exact match.

`dispatchers.go` lines 320–327 (`from`): accepts `len(args) == 1` or
`len(args) == 3 && len(args[0]) > 0 && strings.EqualFold(args[1], "as") && len(args[2]) > 0`.

Rule: `AS` in any case; any non-empty word is a name, **case preserved, not validated**,
after shell-word processing (`ProcessWord`, `shell_parser.go` line 27: `$`, `\` and quotes
are special). Lookup by name is exact, and a purely numeric name collides with a stage
position.

The Podman installed on the development machine (5.7.0) pins `buildah v1.42.0` and
`imagebuilder v1.2.19` (`containers/podman` `v5.7.0` `go.mod` lines 14 and 58).
imagebuilder `v1.2.19` (commit `812868a1428365cb2418a5ea851e59c699f4c6ab`) has the same
`extractNameFromNode` (lines 435–448), the same `ProcessWord(name, …)` stage naming
(lines 351–373) and the same exact `ByName` (line 215), so the rule holds there too.

## The intersection

| Token | BuildKit | Buildah | Proposal |
|---|---|---|---|
| `extra`, `build-1`, `a.b_c` | accepted, unchanged | accepted, unchanged | yes |
| `Builder`, `EXTRA` | accepted, lowercased | accepted, case kept | **no** (treated differently) |
| `1stage`, `0` | rejected | accepted (`0` also names a stage position) | no |
| `-slim`, `_x`, `.x` | rejected | accepted | no |
| `a$b`, `a\b`, `"a"` | rejected | shell-processed | no |
| non-ASCII (e.g. `K` U+212A, lowercases to `k` in Go) | accepted after lowercasing | case kept | no |

So `STAGE_NAME_RE = ^[a-z][a-z0-9_.-]*\Z`, matched against the **raw** token with no case
folding: a token must already be ASCII lowercase, so both builders store the same name.

`as` itself matches the grammar (both builders would accept `FROM x AS as`), but F1
refuses a token equal to `AS` in any case separately (design §4.2): `FROM <ref> as` is F2's
dangling `AS`, never a stage named `as`.
