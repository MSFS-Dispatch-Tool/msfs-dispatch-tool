"""House style check for the app's UI copy: a sentence shown in the app
(a hint, a note, an empty state, an error or confirm dialog) never ends
with a full stop. Periods between sentences are fine; only the last one
goes. The public site (landing, blog, legal pages) is prose and exempt.

    python tools/check_ui_copy.py          # list offenders, exit 1 if any
    python tools/check_ui_copy.py --fix    # remove the final full stops

It checks visible text right before a closing tag and quoted JS strings
that read as a sentence (start with a capital, 9+ characters).
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_FILES = ["templates/index.html", "templates/onboarding.html", "templates/login.html",
             "templates/admin_users.html", "templates/auth_callback.html",
             "templates/auth_forgot_password.html", "templates/auth_reset_password.html",
             "static/js/app.js"]

TEXT_NODE = re.compile(r">([^<>{}]{3,}?[A-Za-z0-9)\]])\.(\s*)</")
JS_STRING = re.compile(r"(['`\"])([A-Z][^'`\"\n]{8,}?[A-Za-z0-9)\]])\.\1")
BEFORE_INTERPOLATION = re.compile(r"([a-z)])\.(\$\{)")


def scan(text):
    body_at = text.find("<body")
    body = text[body_at:] if body_at >= 0 else text
    found = []
    for pattern in (TEXT_NODE, JS_STRING, BEFORE_INTERPOLATION):
        for m in pattern.finditer(body):
            found.append((text[:body_at + m.start()].count("\n") + 1, m.group(0).strip()[-80:]))
    return sorted(found)


def fix(text):
    body_at = text.find("<body")
    head, body = (text[:body_at], text[body_at:]) if body_at >= 0 else ("", text)
    body = TEXT_NODE.sub(lambda m: ">" + m.group(1) + m.group(2) + "</", body)
    body = JS_STRING.sub(lambda m: m.group(1) + m.group(2) + m.group(1), body)
    body = BEFORE_INTERPOLATION.sub(lambda m: m.group(1) + m.group(2), body)
    return head + body


def main():
    offenders = 0
    for name in APP_FILES:
        path = ROOT / name
        text = path.read_text(encoding="utf-8")
        if "--fix" in sys.argv:
            fixed = fix(text)
            if fixed != text:
                path.write_text(fixed, encoding="utf-8")
                text = fixed
        for line, snippet in scan(text):
            offenders += 1
            print(f"{name}:{line}: {snippet}")
    if offenders:
        print(f"\n{offenders} UI sentence(s) end with a full stop. Run with --fix to remove them.")
        return 1
    print("UI copy OK: no sentence ends with a full stop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
