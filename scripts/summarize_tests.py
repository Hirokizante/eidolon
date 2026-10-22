"""Summarize pytest JUnit results without copying full tracebacks into agent context."""

import argparse
from collections import defaultdict
from html import escape
from pathlib import Path
from xml.etree import ElementTree


def summarize(path: Path) -> str:
    if not path.exists():
        return "No pytest report was produced; inspect the test or setup step.\n"
    root = ElementTree.parse(path).getroot()
    cases = list(root.iter("testcase"))
    failed = [case for case in cases if case.find("failure") is not None
              or case.find("error") is not None]
    skipped = sum(case.find("skipped") is not None for case in cases)
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    elapsed = sum(float(suite.get("time", "0")) for suite in suites)
    lines = ["## Pytest results", "",
             f"{len(cases)} cases: {len(failed)} failed, {skipped} skipped. "
             f"Elapsed: {elapsed:.2f}s.", ""]
    if failed:
        lines.extend(["### Failures (first 10; details in job log and JUnit artifact)", ""])
        for case in failed[:10]:
            node = f"{case.get('classname', '')}::{case.get('name', '')}"
            lines.append(f"- <code>{escape(node[:240])}</code>")
        lines.append("")
    modules: dict[str, float] = defaultdict(float)
    for case in cases:
        modules[case.get("classname", "unknown")] += float(case.get("time", "0"))
    lines.extend(["### Slowest modules (summed case time across workers)", "",
                  "| Module | Seconds |", "| --- | ---: |"])
    for module, seconds in sorted(modules.items(), key=lambda item: item[1], reverse=True)[:10]:
        lines.append(f"| <code>{escape(module[:160]).replace('|', '&#124;')}</code> "
                     f"| {seconds:.2f} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    print(summarize(parser.parse_args().report), end="")
