#!/usr/bin/env python3
"""Assertion-style tests for the bootstrap kit (no pytest), following the
agent-readiness-audit skill's test pattern.

No warehouse data, no fixture bundle, no env var: everything here runs on a fresh
clone. Two halves:

  kit_only_tests()   the units that carry a rule — the intake's refusals, the
                     metrics-review stamps, the blocker checkpoint, the
                     model-name/table mapping, the glossary cross-entity gate
  export_tests()     the two export targets, each handed to the tool that has
                     to accept it (tests/roundtrip.py). The dbt half needs
                     dbt-core + dbt-duckdb and SKIPs without them; the
                     `cassis ontology check` half needs CASSIS_API_KEY and
                     SKIPs without it.

Run: python3 tests/test_kit.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402
from harness import case  # noqa: E402
from roundtrip import export_tests  # noqa: E402


def kit_only_tests():
    """No warehouse data needed — these must pass on a fresh clone."""
    import yaml as _yaml

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # --- intake: ask once, validate now, record binding facts ---
        r = subprocess.run([sys.executable, str(KIT / "intake.py"), "questions"],
                           capture_output=True, text=True)
        intake_q = r.stdout
        case("intake questionnaire prints, with the two binding facts in it",
             r.returncode == 0 and "warehouse" in r.stdout.lower()
             and "--absent" in r.stdout and "verbatim" in r.stdout.lower(),
             r.stderr[-200:])

        schema_f = tmp / "schema.json"
        schema_f.write_text("{}")
        for d in ("dbt", "docs", "cards"):
            (tmp / d).mkdir()
        cfg = tmp / "testco.yml"
        base = [sys.executable, str(KIT / "intake.py"), "write",
                "--name", "testco", "--schema", str(schema_f),
                "--input", str(tmp / "dbt"), "--adapter", "dbt",
                "--warehouse-access", "no", "--profile", "sample",
                "--out", str(cfg)]
        happy = base + ["--docs-dir", str(tmp / "docs"),
                        "--dashboards-dir", str(tmp / "cards"),
                        "--absent", "query_history",
                        "--top-question", "how many active users"]
        r = subprocess.run(happy, capture_output=True, text=True)
        case("intake write accepts a complete intake", r.returncode == 0,
             r.stderr[-300:])
        c = _yaml.safe_load(cfg.read_text()) if cfg.exists() else {}
        case("intake config records the binding facts and defaults",
             c.get("warehouse_access") is False and c.get("profile") == "sample"
             and c.get("confirmed_absent") == ["query_history"]
             and c.get("top_questions") == ["how many active users"]
             and c.get("workspace") == "testco-run/workspace"
             and c.get("docs_dirs") == [str(tmp / "docs")], str(c)[:300])
        # `demo` resolves as an alias for `sample`: a config is a recorded
        # fact and keeps working whichever spelling it carries.
        old_cfg = base[:]
        old_cfg[old_cfg.index("sample")] = "demo"
        r = subprocess.run(old_cfg + ["--docs-dir", str(tmp / "docs"),
                                      "--absent", "bi_corpus",
                                      "--absent", "query_history", "--force"],
                           capture_output=True, text=True)
        c_old = _yaml.safe_load(cfg.read_text()) if cfg.exists() else {}
        case("the retired profile name still resolves, and records the new one",
             r.returncode == 0 and c_old.get("profile") == "sample",
             str(c_old.get("profile")) + r.stderr[-160:])
        case("the question says what actually changes, not what we use it for",
             "How much of the warehouse" in intake_q
             and "springboard" not in intake_q and "demo on" not in intake_q,
             intake_q[intake_q.find("7."):][:120])
        # restore the happy-path config the next cases assert against
        r = subprocess.run(happy + ["--force"], capture_output=True, text=True)
        case("intake write points at the next phase",
             "bootstrap.py prep" in r.stdout, r.stdout[-200:])
        r = subprocess.run(happy, capture_output=True, text=True)
        case("intake refuses to overwrite a config without --force",
             r.returncode != 0 and "force" in r.stderr, r.stderr[-200:])
        r = subprocess.run(happy + ["--force"], capture_output=True, text=True)
        case("intake --force redoes the intake", r.returncode == 0, r.stderr[-200:])

        # Where the result lands is asked at intake, not discovered at the end.
        r = subprocess.run(base + ["--docs-dir", str(tmp / "docs"),
                                   "--absent", "bi_corpus",
                                   "--absent", "query_history",
                                   "--emit", "both", "--force"],
                           capture_output=True, text=True)
        c_e = _yaml.safe_load(cfg.read_text()) if cfg.exists() else {}
        case("the output choice is recorded, and the tree is never optional",
             r.returncode == 0 and c_e.get("emit") == ["dbt"], str(c_e.get("emit")))
        r = subprocess.run(base[:base.index("dbt")] + ["dbtdocs"]
                           + base[base.index("dbt") + 1:]
                           + ["--docs-dir", str(tmp / "docs"),
                              "--absent", "bi_corpus", "--absent", "query_history",
                              "--emit", "dbt", "--force"],
                           capture_output=True, text=True)
        case("merging into a dbt project needs the project, not a docs export",
             r.returncode != 0 and "--adapter dbt" in r.stderr, r.stderr[-200:])
        case("the question describes what you get, not what we call it",
             "standalone ontology" in intake_q
             and "merged back into the dbt project" in intake_q
             and "Cassis" not in intake_q.split("9.")[1],
             intake_q.split("9.")[1][:100])
        r = subprocess.run(happy + ["--force"], capture_output=True, text=True)
        r = subprocess.run(happy + ["--force", "--glossary-dir",
                                    str(tmp / "nope")],
                           capture_output=True, text=True)
        case("intake rejects a path that does not exist NOW",
             r.returncode != 0 and "nope" in r.stderr, r.stderr[-200:])
        r = subprocess.run(base + ["--dashboards-dir", str(tmp / "cards"),
                                   "--force"],
                           capture_output=True, text=True)
        case("missing documentation must be declared, never implied",
             r.returncode != 0 and "--absent documentation" in r.stderr,
             r.stderr[-200:])
        r = subprocess.run(happy + ["--force", "--absent", "bi_corpus"],
                           capture_output=True, text=True)
        case("declaring absent material you also passed paths for is a contradiction",
             r.returncode != 0 and "contradicts" in r.stderr, r.stderr[-200:])

        sys.path.insert(0, str(KIT))
        import bootstrap  # noqa: E402
        # Named against the one config that SHIPS: a case named against an
        # untracked config would pass here off a file sitting on our disk and
        # fail on a fresh clone — exactly the ambient state a fresh-clone
        # suite exists to refuse.
        case("a config path in another dir resolves to configs/ by basename",
             bootstrap.resolve_config("elsewhere/example.yml")
             == KIT / "configs" / "example.yml"
             and bootstrap.resolve_config(cfg) == cfg, str(cfg))

        # --- metrics review: every metric on one page, riskiest first ---
        run_d, ws = tmp / "run", tmp / "ws"
        (run_d / "cassis/metrics").mkdir(parents=True)
        (run_d / "cassis/tables/CORE").mkdir(parents=True)
        (run_d / "cassis/domains").mkdir(parents=True)
        (ws / "evidence").mkdir(parents=True)
        (ws / "evidence/metric_candidates.json").write_text(json.dumps([
            {"id": "m1", "expression": "SUM(REVENUE_EUR)", "tables": ["CORE.USERS"],
             "occurrences": 9, "sources": ["dash_1", "dash_2", "q_3"]},
            {"id": "m2", "expression": "AVG(SATISFACTION_SCORE)", "tables": ["CORE.USERS"],
             "occurrences": 3, "sources": ["dash_9"]},
            {"id": "m3", "expression": "AVG(RATING)", "tables": ["CORE.USERS"],
             "occurrences": 4, "sources": ["q_7"]}]))
        (run_d / "cassis/tables/CORE/USERS.yml").write_text(_yaml.safe_dump({
            "name": "CORE.USERS", "domain_path": "core", "grain": ["ID"],
            "columns": [
                {"name": "ID"}, {"name": "REVENUE_EUR"}, {"name": "SATISFACTION_SCORE"},
                # an UNMARKED description = drafted by an agent (or predates
                # stamping); it must never count as the owner's own text
                {"name": "CHURNED",
                 "description": "Whether the user churned, per the agent."},
                {"name": "RATING", "description": "Note de satisfaction 1-5.",
                 "description_source": "warehouse_glossary"},
                {"name": "LAST_SEEN_AT", "description": "Dernière visite.",
                 "description_source": "warehouse_glossary"}]}))
        mm = {
            # 3 corpus sources -> ok
            "revenue_total": {"expression": "SUM(REVENUE_EUR)",
                              "synonyms": ["revenue"]},
            # same corroborated core + a WHERE clause -> ok* (filter flagged)
            "active_revenue": {"expression": "SUM(REVENUE_EUR)",
                               "filters": "STATUS = 'active'", "synonyms": []},
            # 1 corpus source + owner text on RATING -> ok
            "avg_rating": {"expression": "AVG(RATING)", "synonyms": []},
            # 1 corpus source -> VERIFY, synonym-flagged
            "nps": {"expression": "AVG(SATISFACTION_SCORE)", "synonyms": ["nps"]},
            # owner text only -> VERIFY
            "last_seen": {"expression": "MAX(LAST_SEEN_AT)", "synonyms": []},
            # no machine-checkable core -> VERIFY
            "fx_amount": {"expression": "SUM(AMOUNT_EUR * FX_RATE)", "synonyms": []},
            # no corpus match, no STAMPED owner text -> UNGROUNDED
            "churn_rate": {"expression": "COUNT_IF(CHURNED)", "synonyms": ["churn"]},
        }
        for name, spec in mm.items():
            (run_d / f"cassis/metrics/{name}.yml").write_text(_yaml.safe_dump({
                "name": name, "table_schema": "CORE", "table_name": "USERS",
                **spec}))
        r = subprocess.run([sys.executable, str(KIT / "metrics_review.py"),
                            "--run", str(run_d), "--workspace", str(ws)],
                           capture_output=True, text=True)
        case("metrics review runs and writes both artifacts",
             r.returncode == 0 and (run_d / "METRICS-REVIEW.md").exists()
             and (run_d / "metrics_review.json").exists(), r.stderr[-300:])
        rows = json.load(open(run_d / "metrics_review.json"))
        stamps = {x["metric"]: x["stamp"] for x in rows}
        case("two independent evidence units stamp ok (corpus sources, or corpus + owner text)",
             stamps.get("revenue_total") == "ok" and stamps.get("avg_rating") == "ok",
             str(stamps))
        case("one unit stamps VERIFY; none stamps UNGROUNDED",
             stamps.get("nps") == "VERIFY" and stamps.get("last_seen") == "VERIFY"
             and stamps.get("churn_rate") == "UNGROUNDED", str(stamps))
        case("an expression the matcher cannot check is VERIFY, never UNGROUNDED",
             stamps.get("fx_amount") == "VERIFY", str(stamps))
        case("riskiest first: UNGROUNDED leads, synonym-carrying VERIFY precedes plain",
             rows[0]["metric"] == "churn_rate"
             and [x["metric"] for x in rows if x["stamp"] == "VERIFY"][0] == "nps",
             str([x["metric"] for x in rows]))
        combo = next(x for x in rows if x["metric"] == "avg_rating")
        case("evidence names its sources: corpus ids and the owner-text tier",
             any("warehouse_glossary" in e for e in combo["evidence"])
             and any("q_7" in e for e in combo["evidence"]), str(combo["evidence"]))
        case("an UNMARKED description never counts as evidence (self-corroboration guard)",
             stamps.get("churn_rate") == "UNGROUNDED"
             and next(x for x in rows if x["metric"] == "churn_rate")["evidence"]
             == ["none found"], str(stamps))
        active = next(x for x in rows if x["metric"] == "active_revenue")
        md = (run_d / "METRICS-REVIEW.md").read_text()
        case("a corroborated core with a WHERE clause is ok* — filter flagged, never vouched",
             active["stamp"] == "ok" and active["filter_unverified"]
             and any("WHERE not machine-corroborated" in e for e in active["evidence"])
             and "| ok* | active_revenue" in md, str(active)[:200])
        md = (run_d / "METRICS-REVIEW.md").read_text()
        case("the page flags a business synonym riding on a weak stamp",
             "⚠" in md and "1 UNGROUNDED" in md and "present, then proceed" in md.lower())
        r = subprocess.run([sys.executable, str(KIT / "metrics_review.py"),
                            "--run", str(run_d), "--workspace", str(ws), "--strict"],
                           capture_output=True, text=True)
        case("--strict fails while anything is UNGROUNDED", r.returncode == 1)

        # --- the blocker checkpoint: only what changes a number, batch-style ---
        # The fourth stop exists because a stranger running this IS the human
        # the enrichment stages were told they did not have. It shows the two
        # tiers a person can act on and leaves the other four in the file: a
        # page asking about coverage and hygiene is the interrogation the kit
        # refuses.
        qrun = tmp / "qrun"
        qrun.mkdir()
        (qrun / "questions-run.yml").write_text(_yaml.safe_dump([
            {"tier": "blocker",
             "title": "No formula for the savings figure exists anywhere",
             "why_unanswerable": "Selected straight from a passthrough.",
             "affects": {"tables": ["CORE.FACT_TOTALS"]},
             "assumption": "Described as documented, no formula claimed.",
             "error_if_wrong": "Unbounded — it inherits an unaudited "
                               "upstream computation."},
            {"tier": "blocker",
             "title": "Two revenue definitions disagree on tax",
             "why_unanswerable": "Two dashboards compute it differently.",
             "affects": {"metrics": ["revenue_total"],
                         "tables": ["CORE.USERS"]},
             "assumption": "Tax included.",
             "error_if_wrong": "About 20% high on every revenue figure."},
            {"tier": "ruling", "title": "Do cancelled orders count in volume",
             "why_unanswerable": "Both readings are defensible.",
             "assumption": "They are counted.",
             "error_if_wrong": "Volume drops by the cancellation rate."},
            {"tier": "finding", "title": "The docs contradict the SQL",
             "why_unanswerable": "n/a", "assumption": "The SQL wins.",
             "error_if_wrong": "n/a"},
            {"tier": "standard", "title": "Who owns 'active user'",
             "why_unanswerable": "Nobody wrote it down.",
             "assumption": "Thirty-day activity.",
             "error_if_wrong": "Shifts the active-user count."}]))
        r = subprocess.run([sys.executable, str(KIT / "questions.py"), "review",
                            "--run", str(qrun)], capture_output=True, text=True)
        page = (qrun / "QUESTIONS-REVIEW.md")
        case("the review page is written, and says it is a checkpoint",
             r.returncode == 0 and page.exists()
             and "CHECKPOINT" in r.stdout, (r.stderr + r.stdout)[-300:])
        md = page.read_text()
        case("only the two actionable tiers appear; the file keeps the rest",
             "Two revenue definitions" in md and "cancelled orders" in md
             and "contradict the SQL" not in md and "active user" not in md,
             md[:200])
        case("riskiest first: a blocker naming a metric leads the page",
             md.index("Two revenue definitions") < md.index("No formula"), md[:200])
        case("every item carries the assumption and what it costs if wrong",
             md.count("**Assumed for now**") == 3
             and md.count("**If that assumption is wrong**") == 3
             and "add an `answer:` line" in md, md[-300:])
        case("a ruling is offered without insisting",
             "nothing is blocked" in md and "Skipping these is fine" in md,
             md[md.find("## Worth"):][:200])
        r = subprocess.run([sys.executable, str(KIT / "questions.py"), "gate",
                            "--run", str(qrun)], capture_output=True, text=True)
        case("the gate blocks on the unanswered blockers only",
             "2 blocker(s)" in r.stdout and "revenue_total" in r.stdout,
             r.stdout[-200:])

        # an answer folds back into the file the page named, and re-running is
        # the whole interaction — no second page, no per-item chat
        qs = _yaml.safe_load((qrun / "questions-run.yml").read_text())
        qs[1]["answer"] = "Tax excluded. Finance reports net revenue."
        (qrun / "questions-run.yml").write_text(_yaml.safe_dump(qs))
        r = subprocess.run([sys.executable, str(KIT / "questions.py"), "review",
                            "--run", str(qrun)], capture_output=True, text=True)
        md2 = page.read_text()
        first = md2
        subprocess.run([sys.executable, str(KIT / "questions.py"), "review",
                        "--run", str(qrun)], capture_output=True, text=True)
        case("an answered blocker leaves the open list and is recorded as answered",
             "1 blocking" in r.stdout and "## Answered" in md2
             and "Finance reports net revenue" in md2
             and md2.count("**Assumed for now**") == 2, r.stdout[-200:])
        case("the page is re-runnable and deterministic", page.read_text() == first)
        rows = json.load(open(qrun / "questions_review.json"))
        case("the json carries the same items, answer included",
             len(rows) == 3
             and any(x["answer"].startswith("Tax excluded") for x in rows),
             str(rows)[:200])
        r = subprocess.run([sys.executable, str(KIT / "questions.py"), "gate",
                            "--run", str(qrun)], capture_output=True, text=True)
        case("an answered blocker stops blocking its own metric",
             "1 blocker(s) answered" in r.stdout
             and "revenue_total" not in r.stdout, r.stdout[-300:])
        r = subprocess.run([sys.executable, str(KIT / "questions.py"), "merge",
                            "--run", str(qrun)], capture_output=True, text=True)
        case("the answer reaches QUESTIONS.md too",
             "**Answered**: Tax excluded" in (qrun / "QUESTIONS.md").read_text(),
             r.stdout[-200:])

        # --- free-form documentation answers QUESTIONS, never columns ---
        # The measured failure this replaces: the only name-keyed path from
        # prose to a column prefilled 8 columns on a real corpus and all 8
        # were wrong, each stamped as the warehouse owner's own words. So the
        # corpus is pointed at the blocking questions instead, an agent judges
        # whether a passage answers one, and the human accepts it by hand.
        dws = tmp / "dws"
        (dws / "docs").mkdir(parents=True)
        pages = {
            "p1": ("finance/policy.md",
                   "---\ntitle: Finance policy\n---\n\n## Foreign currency\n\n"
                   "The reporting currency for every statement is one currency, "
                   "and that currency is the dollar. Subsidiaries keep their "
                   "local functional currency and are converted at the "
                   "period-end exchange rate.\n"),
            "p2": ("sales/pipeline.md",
                   "## Pipeline amounts\n\nEvery opportunity carries an amount "
                   "and a total. Amounts roll up to the account. Nothing here "
                   "says which currency an amount is in.\n"),
            "p3": ("brand/voice.md",
                   "## Voice\n\nWrite plainly. Nothing about money at all.\n"),
        }
        for pid, (rel, text) in pages.items():
            (dws / "docs" / f"{pid}.txt").write_text(text)
        (dws / "docs" / "index.json").write_text(json.dumps(
            [{"id": pid, "source": f"/x/{rel}", "kind": "text", "rel": rel,
              "chars": len(text)} for pid, (rel, text) in pages.items()]))
        drun = tmp / "drun"
        drun.mkdir()
        (drun / "questions-doc.yml").write_text(_yaml.safe_dump([
            {"tier": "blocker",
             "title": "What currency are the money columns in",
             "why_unanswerable": "No fact table carries a currency column.",
             "affects": {"metrics": ["revenue_total"],
                         "tables": ["CORE.FACT_TOTALS"]},
             "assumption": "One reporting currency, most likely the dollar.",
             "error_if_wrong": "Every dollar metric is silently wrong."},
            {"tier": "blocker",
             "title": "How is the retention movement category computed",
             "why_unanswerable": "The macro body is not in the packet.",
             "affects": {"tables": ["CORE.FACT_TOTALS"]},
             "assumption": "Nothing shipped from it.",
             "error_if_wrong": "Movements land in the wrong category."},
            {"tier": "finding", "title": "A column is declared twice",
             "why_unanswerable": "n/a", "assumption": "The first wins.",
             "error_if_wrong": "n/a"}]))
        r = subprocess.run([sys.executable, str(KIT / "docs_search.py"),
                            "packets", "--run", str(drun),
                            "--workspace", str(dws)],
                           capture_output=True, text=True)
        cands = json.load(open(drun / "docs_candidates.json")) \
            if (drun / "docs_candidates.json").exists() else []
        def pick(needle):
            return next((c for c in cands if needle in c["title"].lower()), {})
        cur = pick("currency")
        case("retrieval searches the blocker tier only, never findings",
             r.returncode == 0 and len(cands) == 2
             and all(c["tier"] == "blocker" for c in cands),
             (r.stderr + str([c["title"] for c in cands]))[-300:])
        case("the passage that answers the question is ranked first",
             bool(cur.get("candidates"))
             and cur["candidates"][0]["source"] == "finance/policy.md",
             str([(p["source"], p["score"]) for p in cur.get("candidates", [])]))
        case("retrieval reports what it hands to a model, before it is spent",
             "characters" in r.stdout and "tokens" in r.stdout
             and "AGENT STEP" in r.stdout, r.stdout[-200:])
        pkt = Path(cur["packet"]).read_text()
        case("the packet withholds the run's own guess and states the stakes",
             "most likely the dollar" not in pkt
             and "silently wrong" in pkt, pkt[:400])
        case("the packet tells the judge that no-answer is the normal answer",
             "not_in_corpus` is the expected result" in pkt
             and "worse than a miss" in pkt, pkt[-400:])
        case("a mandatory standard question carries its own vocabulary",
             "currency" in " ".join(
                 s["item"] for s in __import__("docs_search").standards_of(
                     {"title": "What currency are the money columns in"})),
             str([s["item"] for s in __import__("docs_search").standards_of(
                 {"title": "What currency are the money columns in"})]))

        # Each of the three scoring rules the real corpus taught, pinned by a
        # case that FAILS if the rule is reverted. Measured: an earlier
        # version of these tests passed with all three fixes undone,
        # which is no coverage at all.
        import docs_search as _ds
        wide = {"title": "What currency are the money columns in",
                "why_unanswerable": "amount amount amount amount amount",
                "affects": {"metrics": ["revenue_total", "amount_total"],
                            "tables": ["CORE.FACT_TOTALS", "CORE.AMOUNT_EUR"]}}
        wq = _ds.query_of(wide)
        case("a term's weight is its field's, never a multiple of how often it "
             "occurs",
             wq["amount"] == _ds.FIELD_WEIGHTS["why"]
             and max(wq.values()) <= max(
                 list(_ds.FIELD_WEIGHTS.values()) + [_ds.STANDARD_WEIGHT]),
             str(sorted(wq.items(), key=lambda kv: -kv[1])[:6]))
        case("the objects a blocker gates are not query terms",
             not ({"revenue", "totals", "fact", "eur"} & set(wq)),
             str(sorted(set(wq))))

        # A long section that answers, against many short sections that each
        # mention one query word. Normalizing passage length against the
        # DOCUMENT average makes every passage look short and hands the stub
        # the win; against the passage average, the answer wins.
        lws = tmp / "lws"
        (lws / "docs").mkdir(parents=True)
        answer = ("## Timezone policy\n\n"
                  + ("Every loader is configured by the platform team and the "
                     "configuration is reviewed each quarter by the owning "
                     "group before it ships. ") * 12
                  + "\n\nAll timestamp data in the warehouse is stored in "
                    "UTC, and the session default was overridden to match.\n")
        stubs = "\n".join(f"## Note {i}\n\nA timestamp appears here.\n"
                          for i in range(40))
        lpages = {"L1": ("platform/policy.md", answer),
                  "L2": ("notes/stubs.md", stubs)}
        for pid, (rel, text) in lpages.items():
            (lws / "docs" / f"{pid}.txt").write_text(text)
        (lws / "docs" / "index.json").write_text(json.dumps(
            [{"id": pid, "source": f"/x/{rel}", "kind": "text", "rel": rel,
              "chars": len(text)} for pid, (rel, text) in lpages.items()]))
        lrun = tmp / "lrun"
        lrun.mkdir()
        (lrun / "questions-tz.yml").write_text(_yaml.safe_dump([
            {"tier": "blocker", "title": "What timezone are timestamps stored in",
             "why_unanswerable": "No column mentions a zone.",
             "assumption": "As they arrive.",
             "error_if_wrong": "Day boundaries move."}]))
        r = subprocess.run([sys.executable, str(KIT / "docs_search.py"),
                            "packets", "--run", str(lrun),
                            "--workspace", str(lws), "--passages", "1"],
                           capture_output=True, text=True)
        lc = json.load(open(lrun / "docs_candidates.json"))[0] \
            if (lrun / "docs_candidates.json").exists() else {}
        case("the paragraph that answers beats a stub that shares one word",
             bool(lc.get("candidates"))
             and lc["candidates"][0]["source"] == "platform/policy.md",
             str([(p["source"], p["score"])
                  for p in lc.get("candidates", [])]) + r.stderr[-200:])

        # the verdict comes back and is RENDERED, not applied
        (drun / "docs_answers.yml").write_text(_yaml.safe_dump([
            {"question": cur["key"], "verdict": "answered",
             "answer": "The reporting currency is the dollar.",
             "source": "finance/policy.md:5", "confidence": "high",
             "quote": "The reporting currency for every statement is one "
                      "currency, and that currency is the dollar."},
            {"question": pick("retention movement")["key"],
             "verdict": "not_in_corpus",
             "rejected": "A pipeline page shares the words and defines "
                         "nothing."}]))
        r = subprocess.run([sys.executable, str(KIT / "questions.py"), "review",
                            "--run", str(drun)], capture_output=True, text=True)
        md = (drun / "QUESTIONS-REVIEW.md").read_text()
        case("a candidate is rendered beside its question, with the page it "
             "came from",
             "Candidate answer from your own documentation" in md
             and "`finance/policy.md:5`" in md
             and "nothing is applied until you accept it" in md, md[:300])
        case("a corpus that answers nothing leaves one line and no answer",
             "Searched your documentation: **no page answers this**" in md
             and md.count("Candidate answer") == 1, md[-400:])
        r = subprocess.run([sys.executable, str(KIT / "questions.py"), "gate",
                            "--run", str(drun)], capture_output=True, text=True)
        case("an unconfirmed candidate does not unblock anything",
             "2 blocker(s)" in r.stdout and "revenue_total" in r.stdout,
             r.stdout[-200:])
        r = subprocess.run([sys.executable, str(KIT / "questions.py"), "merge",
                            "--run", str(drun)], capture_output=True, text=True)
        case("the candidate is the only trace an unconfirmed answer leaves",
             "Candidate answer from your own documentation"
             in (drun / "QUESTIONS.md").read_text(), r.stdout[-200:])

        # the eval: a wrong answer and a miss are not the same failure
        labels = tmp / "labels.yml"
        labels.write_text(_yaml.safe_dump([
            {"match": "what currency", "expect": "answered",
             "source_contains": "finance/policy.md"},
            {"match": "retention movement", "expect": "not_in_corpus"}]))
        r = subprocess.run([sys.executable, str(KIT / "docs_search.py"), "score",
                            "--run", str(drun), "--labels", str(labels)],
                           capture_output=True, text=True)
        case("scoring reports precision and recall separately",
             r.returncode == 0 and "precision" in r.stdout
             and "recall" in r.stdout and "FALSE ANSWERS     0" in r.stdout,
             (r.stdout + r.stderr)[-300:])
        bad = _yaml.safe_load((drun / "docs_answers.yml").read_text())
        bad[1] = {"question": bad[1]["question"], "verdict": "answered",
                  "answer": "Invented from a page about something else.",
                  "source": "sales/pipeline.md:1", "confidence": "high"}
        (drun / "docs_answers.yml").write_text(_yaml.safe_dump(bad))
        r = subprocess.run([sys.executable, str(KIT / "docs_search.py"), "score",
                            "--run", str(drun), "--labels", str(labels),
                            "--strict"], capture_output=True, text=True)
        case("answering a question no corpus answers is counted apart, and "
             "fails --strict",
             r.returncode == 1 and "FALSE ANSWERS     1" in r.stdout
             and "worse than a miss" not in r.stdout.split("FALSE")[0],
             r.stdout[-300:])
        r = subprocess.run([sys.executable, str(KIT / "docs_search.py"),
                            "packets", "--run", str(qrun),
                            "--workspace", str(tmp / "no-such-ws")],
                           capture_output=True, text=True)
        case("a run given no documentation says so and changes nothing",
             r.returncode == 0 and "no documentation corpus" in r.stdout
             and not (qrun / "docs_candidates.json").exists(), r.stdout[-200:])

        # --- the output tree has to arrive documented ---
        tpl = (KIT / "templates" / "output-CLAUDE.md")
        case("the kit ships a reader guide it can copy into every output",
             tpl.exists() and "AGENTS.md" in tpl.read_text()
             and "cassis/joins.yml" in tpl.read_text())
        flat = " ".join(tpl.read_text().split())
        case("the reader guide defers the doctrine instead of restating it",
             "The modeling doctrine" in flat and "is not in this file" in flat
             and "defers to that one" in flat
             and "cassis ontology fmt" in flat, flat[:160])

        # --- one count, four places that state it ---
        # Renumbering the checkpoints and leaving a banner behind is a drift
        # a stranger reads before we do.
        drv = (KIT / "bootstrap.py").read_text()
        case("the driver, the kit instructions and the README agree on four "
             "checkpoints",
             all(f"CHECKPOINT {i} of 4" in drv for i in (1, 2, 3, 4))
             and "of 3" not in drv
             and "Four places you must stop" in (KIT / "CLAUDE.md").read_text()
             and "four checkpoints" in (KIT / "README.md").read_text())

        # --- model-name -> materialized-table mapping (the fix-first item) ---
        sys.path.insert(0, str(KIT))
        from common import (glossary_source_token, map_records_to_schema,
                            model_table_candidates)
        case("glossary source token comes from the filename",
             glossary_source_token("a/b/column__geo_area.md") == "geo_area"
             and glossary_source_token("weird.md") == "weird"
             and glossary_source_token(None) == "")
        case("model-name candidates strip one layer prefix and collapse __",
             "SALES_ORDERS" in model_table_candidates("mrt_sales__orders")
             and "EXP_APP_CATALOG_ITEM"
             in model_table_candidates("EXP_APP__CATALOG_ITEM"),
             str(model_table_candidates("mrt_sales__orders")))
        schema_m = {"ANALYTICS.SALES_ORDERS": {},
                    "APP.EXP_APP_CATALOG_ITEM": {},
                    "CORE.USERS": {}, "CORE.DAILY_SALES": {}}
        recs_m = [{"table": "ANALYTICS.MRT_SALES__ORDERS"},
                  {"table": "APP.EXP_APP__CATALOG_ITEM"},
                  {"table": "core.users"},
                  {"table": "MRT_DAILY__SALES"},
                  {"table": "X.MRT_NOPE__MISSING"}]
        n, coll = map_records_to_schema(recs_m, schema_m)
        case("unambiguous records map (prefix, collapse, bare-unique), originals kept",
             n == 3 and not coll
             and recs_m[0]["table"] == "ANALYTICS.SALES_ORDERS"
             and recs_m[0]["model_table"] == "ANALYTICS.MRT_SALES__ORDERS"
             and recs_m[3]["table"] == "CORE.DAILY_SALES"
             and recs_m[4]["table"] == "X.MRT_NOPE__MISSING",
             f"mapped={n} coll={coll}")
        case("direct hits are case-normalized without a model_table mark",
             recs_m[2]["table"] == "CORE.USERS" and "model_table" not in recs_m[2])
        recs_c = [{"table": "ANALYTICS.MRT_SALES__ORDERS"},
                  {"table": "ANALYTICS.FCT_SALES__ORDERS"}]
        n, coll = map_records_to_schema(recs_c, schema_m)
        case("a contested target maps nobody and reports the collision",
             n == 0 and len(coll) == 2
             and recs_c[0]["table"] == "ANALYTICS.MRT_SALES__ORDERS",
             f"mapped={n} coll={coll}")
        recs_d = [{"table": "ANALYTICS.SALES_ORDERS"},
                  {"table": "ANALYTICS.MRT_SALES__ORDERS"}]
        n, coll = map_records_to_schema(recs_d, schema_m)
        case("a directly-claimed table is never overwritten by a mapped record",
             n == 0 and len(coll) == 1, f"mapped={n} coll={coll}")
        # the contested-mapping case: the mart sits in the table's own schema,
        # the int-layer model only bare-matches from another schema — the
        # strictly better match wins, the loser is reported
        recs_t = [{"table": "ANALYTICS.MRT_SALES__ORDERS"},
                  {"table": "INT_CORE.INT_SALES__ORDERS"}]
        n, coll = map_records_to_schema(recs_t, schema_m)
        case("same-schema match beats a cross-schema bare match on a contested table",
             n == 1 and recs_t[0]["table"] == "ANALYTICS.SALES_ORDERS"
             and len(coll) == 1 and coll[0][0] == "INT_CORE.INT_SALES__ORDERS",
             f"mapped={n} coll={coll}")

        # --- the identifier gate must not invent a missing column ---
        # An independent run failed on `identifiers: COALESCE not a column`,
        # because the keyword list carried NULLIF and NULLIFZERO and not
        # COALESCE, and the person running it patched this repo to finish. A
        # function is the token before a `(`; that is structural, and it retires
        # the whole class rather than one name.
        from verify import gate_identifiers
        schema_i = {"CORE.FUNDING": {"FEDERAL_FUNDS": "NUMBER",
                                    "AMOUNT": "NUMBER", "STATUS": "TEXT"}}
        ti = {"tables": {}, "joins": [], "metrics": {}, "domains": {},
              "readmes": [], "unparseable": []}
        for n, expr in (("a", "SUM(COALESCE(FEDERAL_FUNDS, 0))"),
                        ("b", "SAFE_DIVIDE(SUM(AMOUNT), COUNT(DISTINCT STATUS))"),
                        ("c", "SUM(ROUND(AMOUNT, 2))"),
                        ("d", "CAST(SUM(AMOUNT) AS NUMERIC)")):
            ti["metrics"][n] = {"path": f"{n}.yml", "expression": expr,
                               "table_schema": "CORE", "table_name": "FUNDING"}
        f = gate_identifiers(ti, schema_i)
        case("a function call is never read as a column, whatever it is called",
             f == [], str(f))
        ti["metrics"]["e"] = {"path": "e.yml", "expression": "SUM(GHOST_COLUMN)",
                              "table_schema": "CORE", "table_name": "FUNDING"}
        f = gate_identifiers(ti, schema_i)
        case("and a column that really is absent still fails the gate",
             len(f) == 1 and "GHOST_COLUMN" in f[0]["detail"], str(f))

        # --- glossary cross-entity gate (the IRIS class) ---
        from verify import gate_glossary_crossmatch
        wsg = tmp / "wsg"
        wsg.mkdir()
        (wsg / "column_glossary.json").write_text(json.dumps(
            {"REGION_NAME": {"description": "x", "source": "column__geo_area.md"}}))
        tg = {"tables": {
            "ANALYTICS.DIVERSITY": {"path": "d.yml", "columns": [
                {"name": "REGION_NAME", "description_source": "warehouse_glossary",
                 "description_source_detail": "geo_area",
                 "description": "The official name of the region containing the geo area."},
                {"name": "PRODUCT_NAME", "description_source": "warehouse_glossary",
                 "description_source_detail": "product",
                 "description": "Name of the product."},
                {"name": "GEO_AREA_CODE", "description_source": "warehouse_glossary",
                 "description_source_detail": "geo_area",
                 "description": "Code of the geo area."}]},
            "ANALYTICS.GEO_AREA": {"path": "i.yml", "columns": [
                {"name": "REGION_NAME", "description_source": "warehouse_glossary",
                 "description_source_detail": "geo_area",
                 "description": "The region containing the geo area."}]}}}
        f = gate_glossary_crossmatch(tg, wsg)
        case("cross-entity wording flags; entity-in-column or entity-in-table does not",
             len(f) == 1 and "REGION_NAME" in f[0]["detail"]
             and f[0]["file"] == "d.yml", str(f))
        tg["tables"]["ANALYTICS.DIVERSITY"]["columns"][0][
            "description_source"] = "inferred_glossary"
        case("the gate still sees a fill the tier split demoted",
             len(gate_glossary_crossmatch(tg, wsg)) == 1,
             str(gate_glossary_crossmatch(tg, wsg)))

        # --- divergent duplicates: the cross-packet comparison ---
        # Measured on a real run: the warehouse's own yml
        # carried the same sentence on two tables, naming a source field that
        # exists nowhere in the schema. The agent that owned one table
        # corrected it against the SQL; the agent holding the other had that
        # column as check-only and passed it as matching. Per-packet isolation
        # means those two copies were never compared; this gate is the
        # comparison, and the judge pass stops being the only thing that
        # catches the class.
        from verify import gate_divergent_duplicates
        wsd = tmp / "wsd"
        (wsd / "repo").mkdir(parents=True)
        shared = ("Timezone for this feed, taken from the source field "
                  "`agency_timestamp` in the source file.")
        (wsd / "repo" / "tables.json").write_text(json.dumps({"records": [
            {"table": "MART.FEEDS", "column_descriptions": {"TZ": shared}},
            {"table": "MART.CHANNELS", "column_descriptions": {"TZ": shared}},
        ]}))
        td_tree = {"tables": {
            "MART.FEEDS": {"path": "feeds.yml", "columns": [
                {"name": "TZ", "description": "Timezone for this feed, taken "
                 "from the source field `agency_timezone` in the source "
                 "file."}]},
            "MART.CHANNELS": {"path": "channels.yml", "columns": [
                {"name": "TZ", "description": shared}]}}}
        f = gate_divergent_duplicates(td_tree, wsd)
        case("a shared source text corrected on one table flags the verbatim copy",
             len(f) == 1 and f[0]["file"] == "channels.yml"
             and "MART.FEEDS.TZ" in f[0]["detail"], str(f))
        td_tree["tables"]["MART.CHANNELS"]["columns"][0]["description"] = \
            td_tree["tables"]["MART.FEEDS"]["columns"][0]["description"]
        case("both copies corrected: nothing to flag",
             gate_divergent_duplicates(td_tree, wsd) == [],
             str(gate_divergent_duplicates(td_tree, wsd)))
        td_tree["tables"]["MART.FEEDS"]["columns"][0]["description"] = shared
        td_tree["tables"]["MART.CHANNELS"]["columns"][0]["description"] = shared
        case("both copies verbatim: dbt doc-block reuse is not a finding",
             gate_divergent_duplicates(td_tree, wsd) == [],
             str(gate_divergent_duplicates(td_tree, wsd)))

        # --- verify.py --only: the real gates, scoped to the caller's files ---
        # An independent run built three proxy validators because an agent
        # enriching one domain could not run the gates on just its own files;
        # one proxy was trusted over the real tool and retracted twice. The
        # flag scopes the REPORT, never the gates.
        vd = tmp / "vd"
        (vd / "tree" / "tables" / "A").mkdir(parents=True)
        (vd / "tree" / "tables" / "B").mkdir(parents=True)
        (vd / "tree" / "domains").mkdir(parents=True)
        (vd / "tree" / "domains" / "README.md").write_text("# Root\n")
        (vd / "ws").mkdir()
        (vd / "ws" / "schema.json").write_text(json.dumps(
            {"A.ONE": {"X_COL": "INT"}, "B.TWO": {"X_COL": "INT"}}))
        for sch, tab in (("A", "ONE"), ("B", "TWO")):
            (vd / "tree" / "tables" / sch / f"{tab}.yml").write_text(
                _yaml.safe_dump({"name": f"{sch}.{tab}", "domain_path": "",
                                 "columns": [{"name": "X_COL",
                                              "description": "The x col."}]}))
        r = subprocess.run([sys.executable, str(KIT / "verify.py"),
                            "--tree", str(vd / "tree"),
                            "--workspace", str(vd / "ws")],
                           capture_output=True, text=True)
        case("unscoped: the restatement gate sees both files",
             "restatement: 2" in r.stdout, r.stdout[-300:] + r.stderr[-200:])
        r = subprocess.run([sys.executable, str(KIT / "verify.py"),
                            "--tree", str(vd / "tree"),
                            "--workspace", str(vd / "ws"),
                            "--only", "tables/A/*"],
                           capture_output=True, text=True)
        case("--only scopes the report to the caller's own files",
             "restatement: 1" in r.stdout, r.stdout[-300:] + r.stderr[-200:])

        # --- one tier was two producers: split on whether the match is checkable ---
        # The measured failure: a classifier calls any file with three
        # `**Bold**:` lines a glossary, and prose harvested that way filled 8
        # columns on a real corpus with 8 wrong answers — each stamped as the
        # warehouse owner's own words, which is the one tier a metric may
        # corroborate against.
        from common import glossary_tier, parse_column_glossary
        handed = {"description": "x", "source": "g/column__orders.md",
                  "producer": "glossary_dir"}
        guessed = {"description": "x", "source": "wiki/architecture.md",
                   "producer": "context"}
        plain = {"description": "x", "source": "g/glossary.md",
                 "producer": "glossary_dir"}
        case("a dictionary a person handed over, about this table, is the "
             "owner's words",
             glossary_tier(handed, "CORE.ORDERS", "ORDER_ID")
             == "warehouse_glossary"
             and glossary_tier(handed, "CORE.ORDER_ITEMS", "ORDER_ID")
             == "warehouse_glossary")
        case("a classifier's guess about a page is not",
             glossary_tier(guessed, "CORE.ORDERS", "ORDER_ID")
             == "inferred_glossary")
        case("another entity's wording on this table is not either",
             glossary_tier(handed, "CORE.CUSTOMERS", "REGION_NAME")
             == "inferred_glossary")
        case("a glossary that declares no entity is not demoted for it",
             glossary_tier(plain, "CORE.CUSTOMERS", "REGION_NAME")
             == "warehouse_glossary")
        case("the demoted tier is absent from OWNER_TIERS, and the kept one is "
             "in it",
             "inferred_glossary" not in __import__(
                 "metrics_review").OWNER_TIERS
             and "warehouse_glossary" in __import__(
                 "metrics_review").OWNER_TIERS)
        # --- a bad input bundle has to say what is wrong with THEIR file ---
        # Measured by handing the kit four broken bundles. Three failed badly:
        # a header-case mismatch raised `KeyError: 'TABLE_SCHEMA'`, pointing
        # --input at a repo root raised a RuntimeError traceback, and a schema
        # export sharing no table with the project printed "0 reaching the
        # schema" and then "workspace ready", exit 0.
        bad = tmp / "bad"
        (bad / "proj").mkdir(parents=True)
        (bad / "proj" / "dbt_project.yml").write_text(
            "name: p\nversion: '1'\nconfig-version: 2\nprofile: p\n"
            "model-paths: ['models']\n")
        (bad / "proj" / "models").mkdir()
        (bad / "proj" / "models" / "orders.sql").write_text(
            "select 1 as order_id\n")
        lower = bad / "lower.csv"
        lower.write_text("table_schema,table_name,column_name,data_type\n"
                         "main,orders,order_id,INT\n")
        r = subprocess.run([sys.executable, str(KIT / "ingest.py"),
                            "--input", str(bad / "proj"), "--schema", str(lower),
                            "--workspace", str(bad / "w1"), "--adapter", "dbt"],
                           capture_output=True, text=True)
        case("a schema export's header casing is not the user's problem",
             r.returncode == 0 and "1 tables" in r.stdout,
             (r.stdout + r.stderr)[-250:])
        nohdr = bad / "nohdr.csv"
        nohdr.write_text("schema,table,col\nmain,orders,order_id\n")
        r = subprocess.run([sys.executable, str(KIT / "ingest.py"),
                            "--input", str(bad / "proj"), "--schema", str(nohdr),
                            "--workspace", str(bad / "w2"), "--adapter", "dbt"],
                           capture_output=True, text=True)
        case("a genuinely missing column names their file and its real header",
             r.returncode != 0 and "nohdr.csv" in r.stderr
             and "'schema', 'table', 'col'" in r.stderr, r.stderr[-250:])
        r = subprocess.run([sys.executable, str(KIT / "ingest.py"),
                            "--input", str(bad), "--schema", str(lower),
                            "--workspace", str(bad / "w3"), "--adapter", "dbt"],
                           capture_output=True, text=True)
        case("--input at the wrong directory is a sentence, not a traceback",
             r.returncode != 0 and "Traceback" not in r.stderr
             and "dbt_project.yml" in r.stderr, r.stderr[-250:])
        other = bad / "other.csv"
        other.write_text("TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,DATA_TYPE\n"
                         "OTHERDB,NOTHING_ALIKE,COL_A,INT\n")
        r = subprocess.run([sys.executable, str(KIT / "ingest.py"),
                            "--input", str(bad / "proj"), "--schema", str(other),
                            "--workspace", str(bad / "w4"), "--adapter", "dbt"],
                           capture_output=True, text=True)
        case("a bundle where nothing links refuses, instead of reporting ready",
             r.returncode != 0 and "nothing to model" in r.stderr
             and "workspace ready" not in r.stdout, r.stderr[-250:])
        (bad / "docs").mkdir()
        (bad / "docs" / "thing.bin").write_bytes(b"\x00\x01\x02")
        r = subprocess.run([sys.executable, str(KIT / "ingest.py"),
                            "--input", str(bad / "proj"), "--schema", str(lower),
                            "--workspace", str(bad / "w5"), "--adapter", "dbt",
                            "--docs-dir", str(bad / "docs")],
                           capture_output=True, text=True)
        case("a documentation path that yielded nothing says so once",
             r.returncode == 0 and "produced 0 documents" in r.stdout
             and "PosixPath" not in r.stdout, r.stdout[-250:])

        # --- a classifier cannot declare a column dictionary ---
        # Harvesting definitions out of context prose scored 0 for 8 on a real
        # corpus, and a precision floor did not save it: the best-scoring
        # candidate under that rule was a marketing page whose bold labels
        # matched a real column name 30 times out of 30, because in a warehouse
        # of 12,798 column names the column vocabulary is common English.
        cdir = tmp / "cdir"
        cdir.mkdir()
        (cdir / "architecture.md").write_text(
            "# Design\n\n**Status**: the state of a request.\n"
            "**Source**: where it came from.\n"
            "**Owner**: the accountable group.\n")
        idx_c, glos_c, looks = __import__("ingest").ingest_context(
            [cdir], tmp / "cout")
        case("a file shaped like a column dictionary is reported, not harvested",
             glos_c == {} and len(looks) == 1
             and idx_c[0]["kind"] == "glossary", str((glos_c, looks)))
        r = subprocess.run(
            [sys.executable, str(KIT / "ingest.py"), "--input", str(tmp / "dbt"),
             "--schema", str(schema_f), "--adapter", "none",
             "--workspace", str(tmp / "cws"), "--context-dir", str(cdir)],
            capture_output=True, text=True)
        case("and the user is told to pass it with --glossary-dir if it is one",
             "SHAPED like a column dictionary" in r.stdout
             and "--glossary-dir" in r.stdout
             and not (tmp / "cws" / "column_glossary.json").exists(),
             (r.stdout + r.stderr)[-300:])

        # --- instructions written for a model are not documentation ---
        from common import is_agent_instructions
        case("agent instructions are recognized by position, not name alone",
             is_agent_instructions("CLAUDE.md")
             and is_agent_instructions(".cursor/rules/style.md")
             and is_agent_instructions(".github/copilot-instructions.md")
             # the two real handbook pages a name-only rule flagged by mistake
             and not is_agent_instructions("handbook/zendesk/users/agents.md")
             and not is_agent_instructions("handbook/tools/ai/claude.md"))
        adir = tmp / "adir"
        (adir / ".cursor").mkdir(parents=True)
        (adir / "CLAUDE.md").write_text("Always run the tests before pushing.\n")
        (adir / ".cursor" / "rules.md").write_text("Prefer small diffs.\n")
        (adir / "warehouse.md").write_text(
            "## Timezone policy\n\nAll timestamp data in the warehouse is "
            "stored in UTC. Every loader converts on the way in, and the "
            "session default was overridden to match so that a current "
            "timestamp reads UTC too.\n")
        r = subprocess.run(
            [sys.executable, str(KIT / "ingest.py"), "--input", str(tmp / "dbt"),
             "--schema", str(schema_f), "--adapter", "none",
             "--workspace", str(tmp / "aws"), "--docs-dir", str(adir)],
            capture_output=True, text=True)
        didx = json.load(open(tmp / "aws" / "docs" / "index.json"))
        kinds = {e["rel"]: e["kind"] for e in didx}
        case("they are indexed, and indexed as what they are",
             kinds.get("CLAUDE.md") == "agent_instructions"
             and kinds.get(".cursor/rules.md") == "agent_instructions"
             and kinds.get("warehouse.md") == "text"
             and "written for an agent" in r.stdout, str(kinds))
        arun = tmp / "arun"
        arun.mkdir()
        (arun / "questions-a.yml").write_text(_yaml.safe_dump([
            {"tier": "blocker", "title": "What timezone are timestamps stored in",
             "why_unanswerable": "No column mentions a zone.",
             "assumption": "As they arrive.",
             "error_if_wrong": "Day boundaries move."}]))
        r = subprocess.run(
            [sys.executable, str(KIT / "docs_search.py"), "packets",
             "--run", str(arun), "--workspace", str(tmp / "aws")],
            capture_output=True, text=True)
        ac = json.load(open(arun / "docs_candidates.json"))[0]
        case("and no packet quotes a file written to steer an agent",
             "excluded" in r.stdout
             and [p["source"] for p in ac["candidates"]] == ["warehouse.md"],
             str([p["source"] for p in ac["candidates"]]) + r.stdout[-160:])

        gdir = tmp / "gdir"
        gdir.mkdir()
        (gdir / "column__orders.md").write_text("**ORDER_ID**: The order.\n")
        parsed = parse_column_glossary([gdir])
        case("every parsed entry records who called it a glossary",
             parsed["ORDER_ID"]["producer"] == "glossary_dir"
             and parse_column_glossary([gdir], producer="context")[
                 "ORDER_ID"]["producer"] == "context", str(parsed))

        # no corpus at all: corroboration is unsatisfiable — advisory, not noise
        ws2 = tmp / "ws2"
        (ws2 / "evidence").mkdir(parents=True)
        r = subprocess.run([sys.executable, str(KIT / "metrics_review.py"),
                            "--run", str(run_d), "--workspace", str(ws2),
                            "--strict"],
                           capture_output=True, text=True)
        rows2 = json.load(open(run_d / "metrics_review.json"))
        case("with no query corpus every row is VERIFY under one advisory",
             r.returncode == 0
             and {x["stamp"] for x in rows2} == {"VERIFY"}
             and "no query corpus" in (run_d / "METRICS-REVIEW.md").read_text(),
             str({x["stamp"] for x in rows2}))

        # --- the scope decision has to reach the shells ---
        # Regression for the first foreign run: a full-warehouse schema.json
        # and a 50-table agreed scope built 2,183 shells and batched the whole
        # warehouse, because nothing after `scope.py check` read the scope file.
        sys.path.insert(0, str(KIT))
        from common import load_scope
        ws3 = tmp / "ws3"
        (ws3 / "evidence").mkdir(parents=True)
        (ws3 / "schema.json").write_text(json.dumps({
            "COMMON.KEEP": {"ID": "", "AMOUNT": ""},
            "COMMON.PLUMBING": {"ID": ""},
            "COMMON.UNPROVEN": {"ID": ""}}))
        (ws3 / "evidence" / "join_candidates.json").write_text(json.dumps([
            {"left": "COMMON.KEEP", "right": "COMMON.PLUMBING", "on": ["a=b"],
             "provenance": "sql_mined", "occurrences": 1},
            {"left": "COMMON.PLUMBING", "right": "COMMON.UNPROVEN", "on": ["a=b"],
             "provenance": "sql_mined", "occurrences": 1}]))
        scope_f = tmp / "scope.tsv"
        scope_f.write_text("table\tdecision\trule\n"
                           "COMMON.KEEP\tmodel\tlevel2\n"
                           "COMMON.PLUMBING\tread_only\tintermediate\n"
                           "COMMON.UNPROVEN\treview\tmaybe_intermediate\n")
        case("load_scope honours the decision column and skips the header",
             load_scope(scope_f) == {"COMMON.KEEP"}, str(load_scope(scope_f)))
        bare = tmp / "scope-bare.tsv"
        bare.write_text("COMMON.KEEP\trevenue\nCOMMON.PLUMBING\trevenue\n")
        case("load_scope reads a bare list, and a domain in column 2 is not a decision",
             load_scope(bare) == {"COMMON.KEEP", "COMMON.PLUMBING"},
             str(load_scope(bare)))
        run3 = tmp / "run3"
        r = subprocess.run([sys.executable, str(KIT / "skeleton.py"),
                            "--workspace", str(ws3), "--run", str(run3),
                            "--scope", str(scope_f), "--batches", "2"],
                           capture_output=True, text=True)
        shells = sorted(p.name for p in (run3 / "cassis" / "tables").rglob("*.yml"))
        batched = json.load(open(run3 / "batches.json"))["batches"]
        case("skeleton builds a shell only for the tables marked `model`",
             r.returncode == 0 and shells == ["KEEP.yml"]
             and sorted(t for b in batched for t in b) == ["COMMON.KEEP"],
             f"{r.stderr[-200:]} shells={shells}")
        draft = _yaml.safe_load((run3 / "joins_draft.yml").read_text()) or []
        case("a candidate with one foot in the scope survives, marked as a boundary",
             len(draft) == 1 and "outside the modelled scope" in draft[0]["disposition"],
             str(draft))
        empty_f = tmp / "empty.tsv"
        empty_f.write_text("table\tdecision\nCOMMON.KEEP\tread_only\n")
        r = subprocess.run([sys.executable, str(KIT / "skeleton.py"),
                            "--workspace", str(ws3), "--run", str(tmp / "run4"),
                            "--scope", str(empty_f)],
                           capture_output=True, text=True)
        case("a scope that marks nothing for modeling fails loudly",
             r.returncode != 0 and "no table" in (r.stderr + r.stdout),
             (r.stderr + r.stdout)[-200:])

        # --- a macro body must reach the packet that calls it ---
        # Regression from the GitLab run: 8 scoped tables and 159 columns were
        # drafted against `{{ type_of_arr_change(...) }}` call sites while the
        # CASE returning New/Churn/Contraction/Expansion sat unread in macros/.
        sys.path.insert(0, str(KIT))
        from adapters.dbt import collect_macros
        proj = tmp / "mproj"
        (proj / "macros" / "arr").mkdir(parents=True)
        (proj / "dbt_project.yml").write_text("name: mproj\n")
        (proj / "macros" / "arr" / "kind.sql").write_text(
            "{%- macro kind_of_change(arr, previous_arr) -%}\n"
            "  CASE WHEN {{ arr }} = 0 THEN 'Churn' ELSE 'Expansion' END\n"
            "{%- endmacro -%}\n")
        (proj / "macros" / "arr" / "macros.yml").write_text(
            "version: 2\nmacros:\n  - name: kind_of_change\n"
            "    description: How the project classifies an ARR movement.\n")
        mac = collect_macros(proj)
        case("project macros are collected with the project's own description",
             set(mac) == {"kind_of_change"}
             and "Churn" in mac["kind_of_change"]["body"]
             and mac["kind_of_change"]["description"].startswith("How the project"),
             str(mac)[:200])

        wsm = tmp / "wsm"
        (wsm / "repo").mkdir(parents=True)
        (wsm / "evidence").mkdir()
        (wsm / "schema.json").write_text(json.dumps(
            {"COMMON.MRT_SALES__ORDERS": {"USER_STATE": "", "ARR": ""}}))
        (wsm / "repo" / "tables.json").write_text(json.dumps({
            "records": [{"table": "COMMON.MRT_SALES__ORDERS", "dbt_model": "mrt_sales__orders",
                         "sql": "SELECT {{ kind_of_change(arr, prev) }} AS user_state",
                         "macro_calls": ["kind_of_change", "ref"],
                         "column_descriptions": {}}],
            "macros": mac}))
        runm = tmp / "runm"
        (runm / "cassis" / "tables").mkdir(parents=True)
        (runm / "batches.json").write_text(json.dumps(
            {"order": "test", "batches": [["COMMON.MRT_SALES__ORDERS"]]}))
        r = subprocess.run([sys.executable, str(KIT / "dispatch_prep.py"), "packets",
                            "--run", str(runm), "--workspace", str(wsm)],
                           capture_output=True, text=True)
        pk = (runm / "packets" / "batch-0.md").read_text()
        case("the packet carries the macro body, not just the call site",
             r.returncode == 0 and "kind_of_change(arr, previous_arr)" in pk
             and "THEN 'Churn'" in pk
             and "How the project classifies" in pk, r.stderr[-200:] + pk[-300:])
        case("a name the project does not define is not announced as a macro",
             "`ref`" not in pk.split("macros called")[1].split("\n")[0]
             if "macros called" in pk else False, pk[:400])

        # --- a docs corpus must reach disk in full ---
        # Regression from the GitLab handbook pass: doc ids were derived from the
        # BASENAME, and a static-site documentation tree names every leaf
        # _index.md. 4,736 files collapsed onto 3,018 ids, the index still
        # reported 4,736 entries, and nothing said a third of the corpus was
        # never written.
        sys.path.insert(0, str(KIT))
        from ingest import ingest_docs
        tree = tmp / "handbook"
        for section in ("arr", "pipeline", "retention"):
            (tree / "content" / section).mkdir(parents=True)
            (tree / "content" / section / "_index.md").write_text(
                f"# {section}\n\nWhat {section} means here.\n")
        docs_out = tmp / "wsdocs" / "docs"
        idx = ingest_docs([tree], docs_out)
        on_disk = sorted(p.name for p in docs_out.glob("*.txt"))
        case("every page of a docs tree reaches disk, whatever its basename",
             len(idx) == 3 and len(on_disk) == 3
             and len({e["id"] for e in idx}) == 3,
             f"index={len(idx)} on_disk={len(on_disk)}")
        case("the docs index records where each page came from",
             sorted(e["rel"] for e in idx) == ["content/arr/_index.md",
                                               "content/pipeline/_index.md",
                                               "content/retention/_index.md"],
             str([e.get("rel") for e in idx]))
        case("the index's char count matches the bytes actually written",
             sum(e["chars"] for e in idx)
             == sum(len((docs_out / f"{e['id']}.txt").read_text()) for e in idx),
             str([e["chars"] for e in idx]))

        # --- packets must find the record whether it is keyed bare or qualified ---
        # Regression: the lookup used the bare table name only, so on a run whose
        # schema export is qualified every packet said "no dbt model exists for this
        # table" and told the enrichment agent to make no computation claims — for
        # 50 of 50 tables that all had SQL.
        wsp = tmp / "wsp"
        (wsp / "repo").mkdir(parents=True)
        (wsp / "evidence").mkdir()
        (wsp / "schema.json").write_text(json.dumps({
            "COMMON.FCT_MRR": {"MRR": "", "ARR": ""},
            "COMMON.DIM_DATE": {"DATE_ID": ""}}))
        (wsp / "repo" / "tables.json").write_text(json.dumps({"records": [
            {"table": "COMMON.FCT_MRR", "dbt_model": "fct_mrr",
             "sql": "SELECT SUM(mrr) AS mrr FROM prep_charge",
             "column_descriptions": {"MRR": "Monthly Recurring Revenue value"}},
            {"table": "DIM_DATE", "dbt_model": "dim_date",
             "sql": "SELECT date_id FROM dates", "column_descriptions": {}}]}))
        # ^ MRR carries owner text no shell has applied — the case a real
        # run measured: 18 of 20 such columns came back paraphrased
        # because nothing told the agent the owner's words were already there.
        runp = tmp / "runp"
        (runp / "cassis" / "tables").mkdir(parents=True)
        (runp / "batches.json").write_text(json.dumps(
            {"order": "test", "batches": [["COMMON.FCT_MRR", "COMMON.DIM_DATE"]]}))
        r = subprocess.run([sys.executable, str(KIT / "dispatch_prep.py"), "packets",
                            "--run", str(runp), "--workspace", str(wsp)],
                           capture_output=True, text=True)
        pkt = (runp / "packets" / "batch-0.md").read_text() if \
            (runp / "packets" / "batch-0.md").exists() else ""
        case("a qualified record reaches its packet, with the defining SQL",
             r.returncode == 0 and "FROM prep_charge" in pkt
             and "`fct_mrr`" in pkt, r.stderr[-200:] + pkt[:200])
        case("a bare-keyed record still reaches its packet",
             "FROM dates" in pkt and "`dim_date`" in pkt, pkt[-300:])
        case("no packet claims a model is missing when the record has SQL",
             "no dbt model exists" not in pkt
             and "defining SQL reached 2 of 2" in r.stdout, r.stdout[-200:])
        case("unapplied owner text is marked for reuse, not shown as a bare gap",
             "NEEDS DESCRIPTION (owner text present)" in pkt
             and "Monthly Recurring Revenue value" in pkt
             and "Their words, not yours" in pkt
             and "stamp `description_source: repo_text`" in pkt, pkt[-600:])
        case("a column with no owner text stays a plain gap",
             "| NEEDS DESCRIPTION |" in pkt, pkt[-400:])

        # --- a run that never filed its questions must not gate green ---
        # Measured on a real run: finish printed "no questions*.yml found —
        # nothing to merge" then a clean publish gate, while the authored
        # prose pointed readers at a QUESTIONS.md that did not exist.
        nqrun = tmp / "nqrun"
        (nqrun / "cassis" / "tables").mkdir(parents=True)
        (nqrun / "cassis" / "tables" / "t.yml").write_text(
            "description: see QUESTIONS.md for the open currency question\n")
        for verb in ("merge", "gate"):
            r = subprocess.run([sys.executable, str(KIT / "questions.py"), verb,
                                "--run", str(nqrun)], capture_output=True, text=True)
            case(f"questions.py {verb} fails loud when no questions were filed",
                 r.returncode == 1 and "FAIL" in r.stdout
                 and "publishable" not in r.stdout
                 and "mandatory on every run" in r.stdout, r.stdout[-300:])
        case("the failure names the prose pointing at the missing QUESTIONS.md",
             "tables/t.yml" in r.stdout, r.stdout[-300:])

        # --- input defects are said at ingest, not discovered by hand-diffing ---
        # Clean-room: the dbt-matching schema export carried 3,399 rows with
        # data_type empty on every one, and nothing said so.
        sys.path.insert(0, str(KIT))
        import contextlib
        import io
        from ingest import load_schema_any
        bad = tmp / "schema-notypes.csv"
        bad.write_text("TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,DATA_TYPE\n" +
                       "".join(f"M,T{i // 5},C{i},\n" for i in range(25)))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sch, _, _ = load_schema_any(bad)
        case("an export whose data_type column is all empty is warned about",
             "data_type is empty on all 25 columns" in buf.getvalue()
             and sum(len(c) for c in sch.values()) == 25, buf.getvalue()[:200])
        good = tmp / "schema-typed.csv"
        good.write_text("TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME,DATA_TYPE\n" +
                        "".join(f"M,T{i // 5},C{i},TEXT\n" for i in range(25)))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            load_schema_any(good)
        case("a typed export ingests without the warning",
             "WARN" not in buf.getvalue(), buf.getvalue()[:200])

        # --- the mapping warning names the models, not only the count ---
        # Clean-room: "131 of 254 repo records name a table outside schema.json"
        # named the class but not one member.
        ews = tmp / "ews"
        (ews / "repo").mkdir(parents=True)
        (ews / "evidence").mkdir()
        (ews / "schema.json").write_text(json.dumps({"M.KEPT": {"A": ""}}))
        (ews / "repo" / "tables.json").write_text(json.dumps({"records": [
            {"table": "M.GHOSTA"}, {"table": "M.GHOSTB"}, {"table": "M.KEPT"}]}))
        r = subprocess.run([sys.executable, str(KIT / "evidence.py"),
                            "--workspace", str(ews)],
                           capture_output=True, text=True)
        unmapped = ews / "evidence" / "unmapped_models.json"
        case("unmapped repo records land in a file the warning points at",
             r.returncode == 0 and unmapped.exists()
             and "M.GHOSTA" in unmapped.read_text()
             and "unmapped_models.json" in r.stdout
             and "M.GHOSTA" in r.stdout, (r.stdout + r.stderr)[-300:])

        # --- a population finding explains its own trigger ---
        # Clean-room: plain English words ("before", "matching") colliding with
        # a column name fired the gate, and nothing in the report said the
        # collision was the trigger, so a real gap and a coincidence read alike.
        from verify import gate_population_rules
        finds = gate_population_rules(
            {"readmes": {"README.md": "Rows exclude internal servers via the flag."},
             "joins": [], "tables": {"M.FACT": {"domain_path": ""}}},
            {"M.DIM": {"IS_INTERNAL": "BOOLEAN"}, "M.FACT": {"X": ""}},
            tmp / "no-evidence-dir")
        case("a population finding says which words hit which columns, and that "
             "a coincidence reads the same",
             len(finds) == 1
             and "internal" in str(finds[0].get("why"))
             and "M.DIM.IS_INTERNAL" in str(finds[0].get("why"))
             and "reword the" in str(finds[0].get("why")), str(finds)[:300])


def port_tests():
    """The public-tree export: build-repo only, SKIPs on an exported tree
    (the gate tooling it tests is exactly what the export leaves behind)."""
    gate_py = KIT / "tools" / "release_gate.py"
    port_py = KIT / "tools" / "make_public_tree.py"
    if not (gate_py.exists() and port_py.exists()):
        harness.skip("public-tree export", "gate tooling not present (exported tree)")
        return

    import importlib.util

    def load(path, name):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    release_gate = load(gate_py, "release_gate")
    mpt = load(port_py, "make_public_tree")

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "public"
        r = subprocess.run(
            [sys.executable, str(port_py), "--out", str(out)],
            capture_output=True, text=True, cwd=KIT,
        )
        case("public-tree export runs clean on the current tree",
             r.returncode == 0 and "PASS" in r.stdout,
             (r.stdout + r.stderr)[-300:])
        case("no build-repo-only file reaches the export",
             all(not (out / f).exists() for f in mpt.EXCLUDED))
        case("the export is the kit, not a stub",
             (out / "README.md").exists() and (out / "bootstrap.py").exists()
             and (out / "tests" / "test_kit.py").exists())

        planted = Path(td) / "planted"
        planted.mkdir()
        probe = release_gate.FORBIDDEN[0]
        (planted / "note.md").write_text(f"numbers for {probe} q3\n")
        hits = mpt.scan_tree(planted, allowed=set())
        case("the export scan catches a planted customer name, no exemptions",
             len(hits) == 1 and "customer name" in hits[0], str(hits))


def main():
    kit_only_tests()
    print()
    port_tests()
    print()
    export_tests()
    print()
    if harness.failures:
        print(f"{len(harness.failures)} FAILURES: {harness.failures}")
        sys.exit(1)
    print("all tests passed")

if __name__ == "__main__":
    main()
