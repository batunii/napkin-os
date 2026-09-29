#!/usr/bin/env bash
# Read-only repo scan for clan-steward. Prints what exists; changes nothing.
# Usage: scan.sh [root]   (default: cwd)
root="${1:-.}"; cd "$root" || exit 1
name=$(basename "$(pwd)")
echo "== project: $name"
echo "== clan files (depth<=3)"; find . -maxdepth 3 -name '*.clan' -not -path '*/node_modules/*' | sort
[ -f "./$name.clan" ] && echo "ROOT_CLAN=present ($name.clan)" || echo "ROOT_CLAN=missing ($name.clan)"
echo "== component candidates (dirs with a manifest)"
find . -maxdepth 3 \( -name package.json -o -name pyproject.toml -o -name Cargo.toml -o -name go.mod \) \
  -not -path '*/node_modules/*' -not -path '*/.venv/*' | xargs -n1 dirname 2>/dev/null | sort -u
echo "== has code?"; n=$(git ls-files 2>/dev/null | grep -Ec '\.(py|ts|tsx|js|jsx|rs|go|java|rb)$'); echo "source_files=$n"
echo "== tooling"; ls -a | grep -E '^(\.eslintrc.*|eslint.config.*|\.prettierrc.*|biome.json|ruff.toml|\.pre-commit-config.yaml|commitlint.config.*|CODEOWNERS|AGENTS.md|CLAUDE.md|\.github)$'
ls pyproject.toml >/dev/null 2>&1 && grep -E '^\[tool\.(ruff|black|pytest|mypy)' pyproject.toml
echo "== tests"; find . -maxdepth 4 \( -name 'test_*.py' -o -name '*.test.ts*' -o -name '*.spec.ts*' -o -name 'tests' -o -name '__tests__' \) -not -path '*/node_modules/*' | head -8
echo "== fonts in use"
grep -rEho "next/font/(google|local)|font-family:[^;}]{1,80}|@fontsource[-a-z/]*|fonts\.googleapis\.com[^\"')]*" \
  --include='*.css' --include='*.scss' --include='*.html' --include='*.tsx' --include='*.jsx' --include='package.json' --include='tailwind.config.*' \
  --exclude-dir=node_modules --exclude-dir=.next --exclude-dir=dist . 2>/dev/null | sort | uniq -c | sort -rn | head -12
find . -name '*.woff2' -not -path '*/node_modules/*' 2>/dev/null | head -5
echo "== clan freshness (clan is NOT committed; freshness = manifest updated_at vs code commits since)"
for c in $(find . -maxdepth 3 -name '*.clan' -not -path '*/node_modules/*' | sort); do
  upd=$(unzip -p "$c" manifest.yaml 2>/dev/null | awk '/^updated_at:/{print $2; exit}')
  [ -z "$upd" ] && { echo "$c updated_at=unknown"; continue; }
  dir=$(dirname "$c")
  since=$(git log --since="$upd" --oneline -- "$dir" ':!*.clan' 2>/dev/null | wc -l | tr -d ' ')
  tracked=$(git ls-files --error-unmatch "$c" >/dev/null 2>&1 && echo tracked || echo untracked)
  ign=$(git check-ignore -q "$c" && echo ignored || echo not-ignored)
  echo "$c updated_at=$upd code_commits_since=$since git=$tracked/$ign"
done
echo "== metric hints (eval/benchmark/baseline/coverage/cost files and threshold words)"
find . -maxdepth 4 \( -iname '*eval*' -o -iname '*bench*' -o -iname '*baseline*' -o -iname '*checkpoint*' -o -iname 'coverage*' -o -iname 'lighthouse*' -o -iname '.coveragerc' \) \
  -not -path '*/node_modules/*' -not -path '*/.git/*' -not -path '*/target/*' 2>/dev/null | head -12
grep -rEIl "threshold|min_score|pass_rate|p95|latency|coverage|cost_per|health score" --include='*.md' --include='*.yml' --include='*.yaml' --include='*.toml' --include='*.json' \
  --exclude-dir=node_modules --exclude-dir=.git --exclude-dir=target . 2>/dev/null | head -8
