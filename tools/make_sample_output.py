#!/usr/bin/env python3
"""Regenerate sample-output/ from the fixture dbt project.

The sample is real output, not hand-typed: the four-model fixture project
under tests/fixtures/dbt-roundtrip goes through the kit's own deterministic
chain (ingest -> evidence -> skeleton -> prefill -> apply_synthesis -> emit),
exactly as tests/roundtrip.py drives it. The enrichment stages are agent work,
so their output is stood in for by the suite's fixtures — the same stand-ins
the round-trip test ships through `dbt parse` and `cassis ontology check`.

Committed because a reader deciding whether to run the kit believes a file
tree and fifteen lines of the real thing, not adjectives. Regenerate after
any change to emit.py or the fixture:

    python3 tools/make_sample_output.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
OUT = KIT / "sample-output"

sys.path.insert(0, str(KIT / "tests"))
import roundtrip  # noqa: E402

# The excerpt: one table, one metric, one domain README — enough to see the
# shape, small enough to read whole. The full tree is listed in the README.
KEEP = (
    "tables/MAIN/ORDERS.yml",
    "metrics/completed_revenue.yml",
    "domains/commerce/README.md",
)

README = """\
# Sample output

Real output of a run, not a mock-up: the four-model fixture project in
`tests/fixtures/dbt-roundtrip` taken through the kit's deterministic chain,
with the enrichment stages stood in for by the test suite's fixtures (enrichment
is agent work, so a committed sample uses the same stand-ins the round-trip
test hands to `dbt parse` and `cassis ontology check`). Regenerate with
`python3 tools/make_sample_output.py`.

A real run's tree looks exactly like this, with more of everything. The full
emitted tree for these four models:

```
{tree}
```

This directory keeps an excerpt of it — one table, one metric, one domain
README:

{kept}
"""


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        built = roundtrip.build_run(tmp)
        if built is None:
            print("build_run failed — run tests/test_kit.py for the detail")
            return 1
        project, ws, run = built
        r = subprocess.run(
            [sys.executable, str(KIT / "emit.py"), "--emit", "cassis",
             "--run", str(run), "--workspace", str(ws)],
            capture_output=True, text=True)
        if r.returncode:
            print((r.stdout + r.stderr)[-500:])
            return 1
        tree = run / "emit" / "cassis"

        listing = []
        for p in sorted(tree.rglob("*")):
            if p.is_file():
                listing.append(str(p.relative_to(tree)))

        if OUT.exists():
            shutil.rmtree(OUT)
        for rel in KEEP:
            dst = OUT / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(tree / rel, dst)
        (OUT / "README.md").write_text(README.format(
            tree="\n".join(listing),
            kept="\n".join(f"- `{rel}`" for rel in KEEP)))
    print(f"sample-output/ regenerated — {len(KEEP)} files + README")
    return 0


if __name__ == "__main__":
    sys.exit(main())
