#!/root/trueflow/bin/python
# -*- coding: utf-8 -*-
"""
tg_centralise.py - moves every script's own Telegram bot tokens and chat IDs
into tf_config.py, so a future Telegram change is ONE edit.

What it does
  1. Finds each token / chat ID written inside a script as a quoted string.
  2. Uses the tf_config.py setting that already holds that exact value
     (TF_FO, TF_SYSTEM, TELEGRAM_CHAT_ID ...). Only values tf_config does
     not have yet are added, with clear names (TF_BANKNIFTY, TG_TOKEN_...).
  3. Replaces the quoted value in the script with a read from tf_config.
     The VALUE every script uses is identical before and after - nothing
     about who gets which message changes.

Left alone on purpose
  * telegram_ai.py   - your personal assistant bot; it checks who is writing
                       to it, so its chat ID stays exactly as it is.
  * one-off / patch / backup scripts.
  * any token or ID that is NOT a whole quoted string (e.g. inside a longer
    URL) - reported, not touched.

Safety
  Default is a DRY RUN that only prints the plan. With --apply: every file
  (tf_config.py included) gets a .bak_tfc copy first, and each rewritten
  file must parse before it is saved - otherwise that file is skipped.

USAGE
  cd /root/trueflow && bin/python tg_centralise.py            # plan only
  cd /root/trueflow && bin/python tg_centralise.py --apply    # do it
  Undo one file:  cp <file>.bak_tfc <file>
"""
import os, re, sys, ast, glob, shutil, argparse
import requests

os.chdir("/root/trueflow")
sys.path.insert(0, "/root/trueflow")
import tf_config as CFG

TOK = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{30,}")
CHATS = {"-1004405719798", "-1004324853933", "-1003951865342", "-1003744925337",
         "-1004387801904", "-1003944949273", "1202026803"}
NEW_CHAT_NAME = {"-1003944949273": "TF_BANKNIFTY", "-1003951865342": "TF_CHART",
                 "-1004405719798": "TF_BREAKOUTS", "-1004324853933": "TF_FO",
                 "-1003744925337": "TF_US", "-1004387801904": "TF_SYSTEM", "1202026803": "TF_PERSONAL"}
SKIP = re.compile(r"(\.bak|backup|^patch_|^fix_|^apply_|^tg_|^tf_config\.py$|^telegram_ai\.py$|^send_group_guides)")


def existing_name(value):
    for k in dir(CFG):
        if k.isupper() and str(getattr(CFG, k)) == value:
            return k
    return None


def bot_name(tok):
    try:
        j = requests.get("https://api.telegram.org/bot%s/getMe" % tok, timeout=15).json()
        if j.get("ok"):
            return j["result"]["username"]
    except Exception:
        pass
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    apply = ap.parse_args().apply

    files = [f for f in sorted(glob.glob("*.py")) if not SKIP.search(f)]
    srcs = {f: open(f, encoding="utf-8").read() for f in files}

    # every value to centralise -> the tf_config name it will be read from
    names, new = {}, {}
    for f, s in srcs.items():
        for t in set(TOK.findall(s)):
            if t in names:
                continue
            n = existing_name(t)
            if not n:
                u = bot_name(t)
                if not u:
                    print("  ! token in %s did not answer Telegram - left alone" % f)
                    continue
                n = "TG_TOKEN_" + re.sub(r"[^A-Z0-9]", "", u.upper().replace("TRUEFLOW", "").replace("_BOT", "").replace("BOT", ""))
                new[n] = t
            names[t] = n
        for c in CHATS:
            if c in s and c not in names:
                n = existing_name(c) or NEW_CHAT_NAME[c]
                if not existing_name(c):
                    new[n] = c
                names[c] = n

    print("tf_config.py - settings to ADD:" if new else "tf_config.py - nothing to add.")
    for n, v in new.items():
        print("   %-24s = %s" % (n, "<bot token>" if TOK.fullmatch(v) else v))
    print()

    plan, skipped = {}, []
    for f, s in srcs.items():
        out, done, loose = s, [], []
        for v, n in names.items():
            for q in ('"', "'"):
                lit = q + v + q
                if lit in out:
                    out = out.replace(lit, "__import__('tf_config').%s" % n)
                    done.append(n)
            if v in out:
                loose.append(n)
        if out == s:
            if loose:
                skipped.append((f, "value not a whole quoted string: " + ", ".join(sorted(set(loose)))))
            continue
        try:
            ast.parse(out)
        except SyntaxError as e:
            skipped.append((f, "would not parse (line %s) - left alone" % e.lineno))
            continue
        plan[f] = (out, sorted(set(done)), sorted(set(loose)))

    for f, (out, done, loose) in plan.items():
        print("  %-28s -> %s%s" % (f, ", ".join(done), ("   (still inline: %s)" % ", ".join(loose)) if loose else ""))
    for f, why in skipped:
        print("  %-28s  SKIPPED: %s" % (f, why))
    print("\n%d script(s) to update, %d skipped." % (len(plan), len(skipped)))

    if not apply:
        print("\nDRY RUN - nothing written. Run again with --apply to do it.")
        return
    if new:
        shutil.copy2("tf_config.py", "tf_config.py.bak_tfc")
        with open("tf_config.py", "a", encoding="utf-8") as fh:
            fh.write("\n# ── added by tg_centralise.py: Telegram settings that lived inside scripts ──\n")
            for n, v in new.items():
                fh.write('%s = "%s"\n' % (n, v))
        import importlib
        importlib.reload(CFG)
        for n in new:
            assert hasattr(CFG, n), n
        print("tf_config.py updated (backup tf_config.py.bak_tfc)")
    for f, (out, done, loose) in plan.items():
        shutil.copy2(f, f + ".bak_tfc")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write(out)
    print("%d script(s) updated. Each has a .bak_tfc backup." % len(plan))
    # final proof: every rewritten script still compiles, and every name resolves
    import py_compile
    bad = []
    for f in plan:
        try:
            py_compile.compile(f, doraise=True)
        except Exception as e:
            bad.append((f, str(e)[:80]))
    for n in set(n for _, (_, d, _) in plan.items() for n in d):
        if not hasattr(CFG, n):
            bad.append(("tf_config.py", "missing " + n))
    print("VERIFIED - all compile, all settings found." if not bad else "PROBLEMS: %s" % bad)


if __name__ == "__main__":
    main()
