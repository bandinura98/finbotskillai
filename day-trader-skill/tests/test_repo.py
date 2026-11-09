import json
import os
import re
import unittest

from _load import ROOT

# the BIST pack: ten folders (trader-pelosi is US only and was removed)
SKILLS = ["trader-scanner", "trader-news", "trader-entry", "trader-exit", "trader-timemachine",
          "trader-risk", "trader-budget", "trader-broker", "trader-alerts", "trader-analyst"]
ABOUT = ("10 Claude skills for day trading on Borsa İstanbul (BIST): scan charts, track news, time entries "
         "and exits, size positions with BIST ticks, limits and costs, and replay outcomes before you risk a lira.")
# skills on the way to an order carry the safety block
ORDER_PATH = ["trader-scanner", "trader-entry", "trader-exit", "trader-timemachine", "trader-budget",
              "trader-risk", "trader-broker", "trader-alerts"]
V1_ONLY = ["trader-copycat", "trader-screener", "trader-tax", "trader-rebalance"]


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def every_text_file():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            if f.endswith((".md", ".py", ".json", ".csv", ".txt", ".xml")) or f == "LICENSE":
                yield os.path.join(base, f)


class Repo(unittest.TestCase):
    def test_ten_skill_folders_at_the_root(self):
        found = sorted(d for d in os.listdir(ROOT)
                       if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))

    def test_root_matches_the_reel(self):
        root = sorted(d for d in os.listdir(ROOT) if not d.startswith(".git"))
        self.assertEqual(root, sorted([".claude-plugin", "LICENSE", "README.md", "docs", "gereksinim.md", "tests"] + SKILLS))

    def test_frontmatter_name_and_description(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)
            self.assertGreater(len(fm), 200, "%s description is too thin" % s)
            self.assertGreater(len(text.splitlines()), 60, s)

    def test_order_path_skills_carry_the_safety_rule(self):
        for s in ORDER_PATH:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertTrue("not financial advice" in low or "yatırım tavsiyesi değildir" in low, s)
            self.assertTrue("evet" in low or "yes" in low, s)

    def test_every_skill_talks_turkish(self):
        for s in SKILLS:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("Turkish", text, s)
            self.assertIn("BIST", text, s)

    def test_plugin_manifest_points_at_the_root_skills(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            plugin = json.load(fh)
        self.assertEqual(plugin["name"], "day-trader-skill")
        self.assertEqual(plugin["skills"], "./")
        self.assertEqual(plugin["description"], ABOUT)
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json"), encoding="utf-8") as fh:
            market = json.load(fh)
        self.assertEqual(market["name"], "day-trader-skill")
        self.assertEqual(market["plugins"][0]["name"], "day-trader-skill")

    def test_readme_is_the_bist_guide(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = " ".join(fh.read().split())
        self.assertTrue(readme.startswith("# Day Trader skill: Borsa"))
        for line in ("Yatırım tavsiyesi değildir", "docs/BIST-SPEC.md", "emir fişi",
                     "github.com/Jakeschincariol/day-trader-skill"):
            self.assertIn(line, readme)
        for s in SKILLS:
            self.assertIn("/" + s, readme, s)

    def test_no_us_market_left_in_the_tools(self):
        for s in SKILLS:
            for f in os.listdir(os.path.join(ROOT, s)):
                with open(os.path.join(ROOT, s, f), encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                for us in ("America/New_York", "NYSE_HOLIDAYS", "nasdaqtrader", "place_stock_order", "15:55"):
                    self.assertNotIn(us, text, "%s/%s" % (s, f))

    def test_spec_and_tools_share_the_calendar(self):
        with open(os.path.join(ROOT, "docs", "BIST-SPEC.md"), encoding="utf-8") as fh:
            spec = fh.read()
        days = sorted(set(re.findall(r'"(20\d\d-\d\d-\d\d)":', spec)))
        self.assertGreater(len(days), 20)
        for s in SKILLS:
            for f in os.listdir(os.path.join(ROOT, s)):
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(ROOT, s, f), encoding="utf-8") as fh:
                    src = fh.read()
                if "BIST_HOLIDAYS" not in src:
                    continue
                got = sorted(set(re.findall(r'"(20\d\d-\d\d-\d\d)":', src)))
                self.assertEqual(got, days, "%s/%s calendar differs from docs/BIST-SPEC.md" % (s, f))

    def test_no_em_dashes_and_no_personal_email(self):
        for path in every_text_file():
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            rel = os.path.relpath(path, ROOT)
            if not rel.startswith(os.path.join("tests", "fixtures")):  # fixtures are real data, kept as published
                self.assertNotIn("\u2014", text, rel)
            self.assertNotIn("jakeschincariol" + "@" + "gmail.com", text, rel)  # built, so this file passes too

    def test_no_v1_only_skill_is_left_behind(self):
        for path in every_text_file():
            rel = os.path.relpath(path, ROOT)
            if rel.startswith("tests" + os.sep) or rel == "README.md":
                continue  # the README points v1 users to the v1 commit
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            for old in V1_ONLY:
                self.assertNotIn(old, text, rel)

    def test_every_tool_runs_on_python_3_8(self):
        # no 3.9+ only modules or syntax in the tools (the README promises 3.8)
        for s in SKILLS:
            folder = os.path.join(ROOT, s)
            for f in os.listdir(folder):
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    src = fh.read()
                for bad in ("import zoneinfo", "from zoneinfo", ".removeprefix(", ".removesuffix(",
                            "match ", "list[", "dict[", "tuple["):
                    if bad == "match ":
                        self.assertNotRegex(src, r"(?m)^\s+match \w+.*:$", "%s/%s" % (s, f))
                    elif bad.endswith("["):
                        self.assertNotRegex(src, r"(?<![\w.])%s" % re.escape(bad), "%s/%s" % (s, f))
                    else:
                        self.assertNotIn(bad, src, "%s/%s" % (s, f))

    def test_day_trading_limits_ship_in_the_example_config(self):
        with open(os.path.join(ROOT, "trader-budget", "config.example.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        for k in ("max_loss_per_trade_usd", "max_daily_loss_usd", "max_trades_per_day",
                  "min_reward_risk", "require_stop", "no_entries_after", "flat_by"):
            self.assertIn(k, cfg, k)
        self.assertIs(cfg["require_stop"], True)
        self.assertIs(cfg.get("allow_short", False), False)
        self.assertIs(cfg.get("allow_margin", False), False)

    def test_paper_trading_is_the_default(self):
        with open(os.path.join(ROOT, "trader-budget", "config.example.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertIs(cfg["paper"], True)
        self.assertIs(cfg.get("pdt_guard", False), False)
        self.assertEqual(cfg.get("flat_by"), "17:50")
        self.assertIs(cfg.get("allow_options", False), False)


if __name__ == "__main__":
    unittest.main()
