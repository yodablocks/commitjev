"""Commits with known defects, for measuring what commitjev actually catches.

Each case is one commit built on the same small base project, carrying exactly
one defect or none. The defect is the rule that should fire. Everything else
about the commit is meant to look ordinary, because a defect that only shows up
in an obviously broken commit tells you nothing about real ones.

These are written by hand, so they measure whether a rule can fire at all and
where it sits relative to clean commits. They are not a sample of anyone's real
history and the separations they produce are not accuracy numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BASE_FILES: dict[str, str] = {
    "README.md": """# ledger

A small ledger service.

## Running

    python3 -m app.server

Configuration comes from the environment.
""",
    "requirements.txt": "requests==2.31.0\n",
    "app/client.py": '''"""HTTP access to the upstream ledger."""

import json
import urllib.request

TIMEOUT_SECONDS = 30
MAX_RETRIES = 3
BASE_URL = "https://ledger.example.com"


def validate_account(account_id):
    """Reject anything that is not a positive integer account id."""
    if not isinstance(account_id, int) or account_id <= 0:
        raise ValueError(f"bad account id: {account_id!r}")
    return account_id


def fetch_balance(account_id):
    validate_account(account_id)
    url = f"{BASE_URL}/accounts/{account_id}/balance"
    with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read())["balance"]


def retry(operation):
    """Run operation, retrying on any failure up to MAX_RETRIES times."""
    last = None
    for _ in range(MAX_RETRIES):
        try:
            return operation()
        except Exception as exc:
            last = exc
    raise last
''',
    "app/report.py": '''"""Turning balances into a printable statement."""

from app.client import fetch_balance


def statement(account_ids):
    lines = []
    for account_id in account_ids:
        lines.append(f"{account_id}: {fetch_balance(account_id):.2f}")
    return "\\n".join(lines)
''',
    "tests/test_client.py": '''from app.client import validate_account


def test_validate_rejects_zero():
    try:
        validate_account(0)
    except ValueError:
        return
    raise AssertionError("expected ValueError")
''',
}


# Joined at runtime so that no string shaped like a live AWS key is committed
# in this file. The fixture repository still gets the whole thing, which is what
# the credential rule and the credential regex have to see.
FAKE_KEY_ID = "AKIA" + "J7QRS2VWXYZ4NBQA"
FAKE_SECRET = "kQ8vTn2LpR5wYc3JmD7Fb" + "Hx1ZaUeNs4GtVi6Ow9R"


@dataclass
class Case:
    name: str
    defect: str | None          # the rule id that should fire, or None
    message: str
    writes: dict[str, str] = field(default_factory=dict)
    deletes: tuple[str, ...] = ()


CASES: tuple[Case, ...] = (
    Case(
        name="clean_feature",
        defect=None,
        message=(
            "Give retry a delay between attempts\n\n"
            "retry() looped straight through its three attempts, so a request that "
            "failed because the upstream was busy failed three times in a row. It "
            "now waits a second between attempts."
        ),
        writes={
            "app/client.py": BASE_FILES["app/client.py"].replace(
                '''import json
import urllib.request''',
                '''import json
import time
import urllib.request''',
            ).replace(
                """        except Exception as exc:
            last = exc
    raise last""",
                """        except Exception as exc:
            last = exc
            time.sleep(1)
    raise last""",
            ),
        },
    ),
    Case(
        name="clean_docs",
        defect=None,
        message=(
            "Say in the README which environment variables the service reads\n\n"
            'The configuration section said "comes from the environment" without '
            "naming anything, so a new deployment had to be read out of the code."
        ),
        writes={
            "README.md": BASE_FILES["README.md"].replace(
                "Configuration comes from the environment.",
                """Configuration comes from the environment:

- `LEDGER_BASE_URL`, the upstream ledger
- `LEDGER_TIMEOUT`, seconds to wait on a request""",
            ),
        },
    ),
    Case(
        name="clean_test",
        defect=None,
        message=(
            "Cover the negative and non-integer cases in validate_account\n\n"
            "The existing test only checked zero."
        ),
        writes={
            "tests/test_client.py": BASE_FILES["tests/test_client.py"]
            + '''

def test_validate_rejects_negative():
    try:
        validate_account(-4)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_validate_rejects_a_string():
    try:
        validate_account("12")
    except ValueError:
        return
    raise AssertionError("expected ValueError")
''',
        },
    ),
    Case(
        name="wrong_message",
        defect="message_describes_diff",
        message=(
            "Fix a typo in the README\n\n"
            "The running section said 'python' where it meant 'python3'."
        ),
        writes={
            "app/client.py": BASE_FILES["app/client.py"]
            .replace("TIMEOUT_SECONDS = 30", "TIMEOUT_SECONDS = 2")
            .replace(
                'BASE_URL = "https://ledger.example.com"',
                'BASE_URL = "https://ledger-eu.example.com"',
            ),
        },
    ),
    Case(
        name="vague_message",
        defect="message_describes_diff",
        message="update stuff",
        writes={
            "app/report.py": BASE_FILES["app/report.py"].replace(
                """def statement(account_ids):
    lines = []
    for account_id in account_ids:
        lines.append(f"{account_id}: {fetch_balance(account_id):.2f}")
    return "\\n".join(lines)""",
                """def statement(account_ids, currency="USD"):
    lines = [f"Statement in {currency}"]
    for account_id in sorted(account_ids):
        lines.append(f"{account_id}: {fetch_balance(account_id):.2f} {currency}")
    lines.append(f"{len(account_ids)} accounts")
    return "\\n".join(lines)""",
            ),
        },
    ),
    Case(
        name="bundled",
        defect="single_purpose",
        message=(
            "Fix the statement ordering and rename the client module\n\n"
            "Accounts came out in whatever order they were passed in, and client.py "
            "is really an upstream adapter, so it is now upstream.py."
        ),
        writes={
            "app/report.py": BASE_FILES["app/report.py"]
            .replace("from app.client import fetch_balance",
                     "from app.upstream import fetch_balance")
            .replace("for account_id in account_ids:",
                     "for account_id in sorted(account_ids):"),
            "app/upstream.py": BASE_FILES["app/client.py"],
            "tests/test_client.py": BASE_FILES["tests/test_client.py"].replace(
                "from app.client import", "from app.upstream import"
            ),
        },
        deletes=("app/client.py",),
    ),
    Case(
        name="hidden_change",
        defect="undisclosed_change",
        message=(
            "Log every balance lookup\n\n"
            "Support could not tell which account a failing request was for."
        ),
        writes={
            "app/client.py": BASE_FILES["app/client.py"]
            .replace(
                '''import json
import urllib.request''',
                '''import json
import logging
import urllib.request

log = logging.getLogger(__name__)''',
            )
            .replace(
                """def fetch_balance(account_id):
    validate_account(account_id)""",
                """def fetch_balance(account_id):
    log.info("fetching balance for %s", account_id)""",
            )
            .replace("TIMEOUT_SECONDS = 30", "TIMEOUT_SECONDS = 5"),
        },
    ),
    Case(
        name="silent_removal",
        defect="unexplained_removal",
        message="Tidy up the client\n\nGeneral cleanup of app/client.py.",
        writes={
            "app/client.py": BASE_FILES["app/client.py"]
            .replace(
                '''def retry(operation):
    """Run operation, retrying on any failure up to MAX_RETRIES times."""
    last = None
    for _ in range(MAX_RETRIES):
        try:
            return operation()
        except Exception as exc:
            last = exc
    raise last
''',
                "",
            )
            .replace("MAX_RETRIES = 3\n", ""),
        },
    ),
    Case(
        name="debug_left",
        defect="debug_leftovers",
        message=(
            "Let statement() take a page of accounts\n\n"
            "Long account lists timed out, so statement() now takes an offset and a "
            "page size."
        ),
        writes={
            "app/report.py": '''"""Turning balances into a printable statement."""

from app.client import fetch_balance

PAGE_SIZE = 1  # TODO put this back to 50 before merging


def statement(account_ids, offset=0):
    page = account_ids[offset:offset + PAGE_SIZE]
    print("PAGE", page)
    lines = []
    for account_id in page:
        # balance = fetch_balance(account_id)
        # lines.append(f"{account_id}: {balance:.2f}")
        lines.append(f"{account_id}: {fetch_balance(account_id):.2f}")
    print("done")
    return "\\n".join(lines)
''',
        },
    ),
    Case(
        name="new_dep",
        defect="new_dependency",
        message=(
            "Parse upstream responses faster\n\n"
            "Decoding a large statement was the slowest part of a lookup."
        ),
        writes={
            "app/client.py": BASE_FILES["app/client.py"]
            .replace("import json\n", "import orjson\n")
            .replace('json.loads(response.read())["balance"]',
                     'orjson.loads(response.read())["balance"]'),
            "requirements.txt": "requests==2.31.0\norjson==3.10.7\n",
        },
    ),
    Case(
        name="secret",
        defect="secret_material",
        message=(
            "Read the upstream credentials at import time\n\n"
            "The client needed the signing credentials before its first request."
        ),
        writes={
            "app/client.py": BASE_FILES["app/client.py"].replace(
                'BASE_URL = "https://ledger.example.com"',
                'BASE_URL = "https://ledger.example.com"\n'
                f'AWS_ACCESS_KEY_ID = "{FAKE_KEY_ID}"\n'
                f'AWS_SECRET_ACCESS_KEY = "{FAKE_SECRET}"',
            ),
        },
    ),
)
