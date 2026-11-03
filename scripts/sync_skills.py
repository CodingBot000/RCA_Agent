"""Copy canonical skill files into both agent discovery paths or detect drift."""

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("spec-development", "code-review", "regression-analysis")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    errors = []
    for name in NAMES:
        source = ROOT / "skills" / name / "SKILL.md"
        for platform in (".codex", ".claude"):
            target = ROOT / platform / "skills" / name / "SKILL.md"
            if args.write:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
            elif not target.is_file() or target.read_bytes() != source.read_bytes():
                errors.append(str(target.relative_to(ROOT)))
    if errors:
        print("Skill drift: " + ", ".join(errors))
        return 1
    print("Skills synchronized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
