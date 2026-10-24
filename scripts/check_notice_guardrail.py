"""One-off evidence check (S15): apply the published policy guardrail to the patient notice, once per language.

Not in the runtime path: the app never runs this, and the notice renders with no model or network call. It reads
the exact text each printed page shows from web/js/notice.js (noticeText, via node), so what is checked is what
ships. Re-run it whenever that text changes and record the outcome in docs/decisions.md (S15).

    ( set -a; source .env; set +a; DRY_RUN=0 .venv/bin/python scripts/check_notice_guardrail.py )

Needs GUARDRAIL_ID, GUARDRAIL_VERSION and AWS credentials in the environment. Prints per-language status, policy
units and an estimated cost only: never the raw AWS response, the guardrail id or any account id.
Exit 0 when every language passed, 1 when any intervened, 2 when it could not run.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ApplyGuardrail list price, USD per 1,000 text units (one unit is up to 1,000 characters). Estimate only.
PRICE_PER_1K = {"contentPolicyUnits": 0.15, "topicPolicyUnits": 0.15, "sensitiveInformationPolicyUnits": 0.10,
                "contextualGroundingPolicyUnits": 0.10, "wordPolicyUnits": 0.0}


def notice_texts() -> list[tuple[str, str]]:
    node = shutil.which("node")
    if not node:
        raise SystemExit("node not found: it reads the notice text from web/js/notice.js")
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("api.js", "dom.js", "state.js", "notice.js"):
            shutil.copy(ROOT / "web" / "js" / name, Path(tmp) / name)
        (Path(tmp) / "package.json").write_text('{"type": "module"}')
        script = (
            "globalThis.window = { addEventListener() {} };\nglobalThis.navigator ??= { onLine: true };\n"
            f"const N = await import({json.dumps((Path(tmp) / 'notice.js').as_uri())});\n"
            "console.log(JSON.stringify(N.NOTICE.map((n) => [n.lang, N.noticeText(n)])));"
        )
        out = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=20)
    if out.returncode:
        raise SystemExit(f"could not read web/js/notice.js: {out.stderr.strip()}")
    return [tuple(x) for x in json.loads(out.stdout)]


def main() -> int:
    gid, ver = os.environ.get("GUARDRAIL_ID", "").strip(), os.environ.get("GUARDRAIL_VERSION", "").strip()
    if os.environ.get("DRY_RUN", "0") == "1" or not (gid and ver):
        print("refusing: needs DRY_RUN=0 with GUARDRAIL_ID and GUARDRAIL_VERSION set (source .env in a subshell)")
        return 2
    from app.ai import client as ai
    runtime = ai._runtime()
    failed, total = False, 0.0
    print(f"guardrail version {ver} (id from GUARDRAIL_ID), source OUTPUT, guard_content only")
    for lang, text in notice_texts():
        try:
            resp = runtime.apply_guardrail(guardrailIdentifier=gid, guardrailVersion=ver, source="OUTPUT",
                                           content=[{"text": {"text": text, "qualifiers": ["guard_content"]}}])
        except Exception as e:  # noqa: BLE001 - report the error class only, never the raw response
            code = getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)
            print(f"{lang}: error {code}")
            return 2
        usage = resp.get("usage", {})
        usd = sum(usage.get(k, 0) * p / 1000 for k, p in PRICE_PER_1K.items())
        total += usd
        intervened = resp.get("action") == "GUARDRAIL_INTERVENED"
        failed |= intervened
        reasons = sorted({k for a in resp.get("assessments", []) for k in a if k.endswith("Policy")})
        units = {k: v for k, v in usage.items() if v}
        print(f"{lang}: {'intervened ' + ','.join(reasons) if intervened else 'passed'} · {len(text)} chars · "
              f"units {units} · est ${usd:.5f}")
    print(f"total est ${total:.5f}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
