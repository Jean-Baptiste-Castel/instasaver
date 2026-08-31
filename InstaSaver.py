#!/usr/bin/env python3
"""
InstaSaver
Archive your Instagram saved posts to a folder on your computer.

Wraps Instaloader (github.com/instaloader/instaloader) with a supervisor that
handles Instagram's rate limiting: when the download is paused, the app waits,
explains why, and resumes on its own.
"""

import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import queue
from datetime import datetime, timedelta
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog
    import tkinter.font as tkfont
except ImportError:  # allows the logic below to be imported and tested headless
    tk = None

APP_NAME = "InstaSaver"
APP_VERSION = "1.1"

# ----------------------------------------------------------------------------
# Apple dark mode system colours
# ----------------------------------------------------------------------------
BG = "#1C1C1E"          # window background
CARD = "#2C2C2E"        # elevated card
CARD_HI = "#3A3A3C"     # card, pressed
SEPARATOR = "#38383A"
LABEL = "#FFFFFF"
LABEL_2 = "#98989E"     # secondary label
LABEL_3 = "#636366"     # tertiary label
BLUE = "#0A84FF"
BLUE_HI = "#409CFF"
GREEN = "#30D158"
ORANGE = "#FF9F0A"
ORANGE_DIM = "#3A2E17"  # orange at low opacity over BG
RED = "#FF453A"
FILL_TRACK = "#48484A"

WIN_W, WIN_H = 520, 742
MARGIN = 24
CONTENT_W = WIN_W - MARGIN * 2

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = os.name == "nt"


def support_dir():
    """Where this app keeps its own preferences and log."""
    if IS_WINDOWS:
        base = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")) / APP_NAME


def instaloader_dirs():
    """Where Instaloader keeps its session files, in its own order per platform."""
    if IS_WINDOWS:
        base = os.getenv("LOCALAPPDATA")
        return [Path(base) / "Instaloader"] if base else []
    return [Path(os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")) / "instaloader",
            Path.home() / "Library" / "Application Support" / "instaloader"]


SUPPORT_DIR = support_dir()
PREFS_FILE = SUPPORT_DIR / "prefs.json"
HAND = "pointinghand" if IS_MAC else "hand2"
DISPLAY_FACES = ["SF Pro Display", "SF Pro", "Segoe UI Variable Display",
                 "Segoe UI", "Helvetica Neue", "DejaVu Sans"]
TEXT_FACES = ["SF Pro Text", "SF Pro", "Segoe UI Variable Text",
              "Segoe UI", "Helvetica Neue", "DejaVu Sans"]

BROWSERS = ["Chrome", "Safari", "Firefox", "Brave", "Edge", "Arc", "Opera", "Vivaldi"]

# Instagram lifts these blocks on its own. Minutes to hours, 24h worst case.
BACKOFF = [20 * 60, 45 * 60, 90 * 60, 3 * 3600, 6 * 3600, 12 * 3600]
BACKOFF_RATE_LIMIT = [10 * 60, 30 * 60, 60 * 60, 2 * 3600, 4 * 3600, 8 * 3600]

PROGRESS_RE = re.compile(r"\[\s*(\d+)\s*/\s*(\d+)\s*\]")


# ----------------------------------------------------------------------------
# Pure helpers (no UI, unit testable)
# ----------------------------------------------------------------------------
def backoff_for(attempt, rate_limited=False):
    """Seconds to wait before retry number `attempt` (1-based)."""
    table = BACKOFF_RATE_LIMIT if rate_limited else BACKOFF
    return table[min(max(attempt, 1) - 1, len(table) - 1)]


def parse_progress(line):
    """Pull (done, total) out of an Instaloader progress line, else None."""
    m = PROGRESS_RE.search(line)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def format_countdown(seconds):
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return "%dh %02dm" % (seconds // 3600, (seconds % 3600) // 60)
    return "%d:%02d" % (seconds // 60, seconds % 60)


def format_count(n):
    return "{:,}".format(int(n))


def shorten_path(p, limit=44):
    s = str(p).replace(str(Path.home()), "~")
    if len(s) <= limit:
        return s
    parts = Path(s).parts
    if len(parts) > 3:
        s = os.path.join(parts[0], "...", parts[-2], parts[-1])
    return s if len(s) <= limit else "..." + s[-(limit - 3):]


DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%Y-%m", "%m/%Y", "%Y")


def parse_date(text, as_end=False):
    """Accepts 2024, 2024-06, 2024-06-15 and the day first variants.

    as_end pushes a partial date to the last moment of that period, so a range
    written as 2024 to 2025 covers both years completely.
    """
    text = (text or "").strip()
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            d = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if not as_end:
            return d
        if fmt == "%Y":
            return d.replace(month=12, day=31, hour=23, minute=59, second=59)
        if fmt in ("%Y-%m", "%m/%Y"):
            nxt = d.replace(day=28) + timedelta(days=4)
            last = nxt - timedelta(days=nxt.day)
            return last.replace(hour=23, minute=59, second=59)
        return d.replace(hour=23, minute=59, second=59)
    return None


def check_range(from_text, to_text):
    """Returns (start, end, problem). problem is None when the range is usable."""
    start = parse_date(from_text, as_end=False)
    end = parse_date(to_text, as_end=True)
    if from_text.strip() and start is None:
        return None, None, "Check the From date, try 2024-06-15"
    if to_text.strip() and end is None:
        return None, None, "Check the To date, try 2024-06-15"
    if start is None and end is None:
        return None, None, "Enter at least one date"
    if start and end and start > end:
        return None, None, "The From date is after the To date"
    return start, end, None


def default_folder():
    """Reuse an existing Instaloader archive if one is already on disk."""
    for candidate in (
        Path.home() / "instagram-saved",
        Path.home() / "Documents" / "instagram-saved",
        Path.home() / "Pictures" / "Instagram Saved",
    ):
        if candidate.is_dir():
            return candidate
    return Path.home() / "Pictures" / "Instagram Saved"


def find_sessions():
    """Instaloader session files already on this Mac."""
    out = []
    for base in instaloader_dirs():
        if base.is_dir():
            for f in sorted(base.glob("session-*")):
                out.append((f.name[len("session-"):], f))
    return out


def load_prefs():
    try:
        return json.loads(PREFS_FILE.read_text())
    except Exception:
        return {}


def save_prefs(prefs):
    try:
        SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
        PREFS_FILE.write_text(json.dumps(prefs, indent=2))
    except Exception:
        pass


def safe_folder_name(name, limit=60):
    """Instagram collection names can contain anything, folders cannot."""
    cleaned = "".join(" " if ch in '/\\:*?"<>|' else ch for ch in (name or ""))
    cleaned = " ".join(cleaned.split()).strip(". ")
    if not cleaned:
        cleaned = "Untitled"
    return cleaned[:limit]


def friendly_connect_error(err, browser):
    """Turns a library exception into something a person can act on."""
    text = str(err).strip()
    low = text.lower()
    if "no instagram login" in low or "not logged in" in low or "test_login" in low:
        return ("No Instagram login found in %s. Open instagram.com in %s, sign in, "
                "then try again." % (browser, browser))
    if "could not find" in low or "not installed" in low or "no such file" in low \
            or "profile" in low:
        return ("%s does not seem to be installed on this Mac, or it has never been "
                "used. Try another browser." % browser)
    if "keychain" in low or "safe storage" in low or "password" in low \
            or "decrypt" in low:
        return ("macOS did not allow access to %s's saved data. Try again and press "
                "Allow when it asks." % browser)
    if "operation not permitted" in low or "permission" in low:
        return ("macOS blocked access to %s's files. Safari in particular needs Full "
                "Disk Access. Chrome or Firefox is usually easier." % browser)
    if not text:
        return ("%s did not hand over a session. Make sure instagram.com is open and "
                "signed in there, then try again." % browser)
    return text[:200]


# picker row that mirrors the whole library, collections as folders
MIRROR = "__mirror__"


class StopRequested(Exception):
    pass


# ----------------------------------------------------------------------------
# Download engine
# ----------------------------------------------------------------------------
class Engine:
    """Runs Instaloader in a worker thread and reports back through a queue."""

    def __init__(self, out_queue):
        self.q = out_queue
        self.stop_flag = threading.Event()
        self.thread = None
        self.caffeinate = None

    # -- messaging ----------------------------------------------------------
    def emit(self, kind, **data):
        self.q.put(dict(kind=kind, **data))

    # -- login --------------------------------------------------------------
    def connect_session(self, username, session_file):
        import instaloader
        L = instaloader.Instaloader()
        L.load_session_from_file(username, str(session_file))
        who = L.test_login()
        if not who:
            raise RuntimeError("That saved session has expired.")
        return who

    def connect_browser(self, browser):
        import browser_cookie3
        import instaloader

        getter = getattr(browser_cookie3, browser.lower(), None)
        if getter is None:
            raise RuntimeError("%s is not supported." % browser)
        jar = getter(domain_name="instagram.com")

        L = instaloader.Instaloader()
        L.context._session.cookies.update(jar)
        who = L.test_login()
        if not who:
            raise RuntimeError(
                "No Instagram login found in %s. Open instagram.com in %s, "
                "sign in, then try again." % (browser, browser)
            )
        L.context.username = who
        SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
        session_dir = (instaloader_dirs() or [SUPPORT_DIR])[0]
        session_dir.mkdir(parents=True, exist_ok=True)
        L.save_session_to_file(str(session_dir / ("session-" + who)))
        return who

    def _run_collections(self, L, collections, mode, start_date, end_date, post_filter):
        total = sum(c.get("count") or 0 for c in collections) or None
        seen = 0
        for col in collections:
            if self.stop_flag.is_set():
                raise StopRequested()
            target = safe_folder_name(col["name"])
            self.emit("log", text="Collection: %s" % col["name"])
            fresh = 0
            for post in self.collection_posts(L, col["id"]):
                if self.stop_flag.is_set():
                    raise StopRequested()
                seen += 1
                self.emit("progress", done=seen, total=total or seen)
                if not post_filter(post):
                    continue
                downloaded = L.download_post(post, target=target)
                if mode == "new":
                    # stop this collection once we reach what we already hold
                    fresh = 0 if downloaded else fresh + 1
                    if fresh >= 8:
                        self.emit("log", text="%s is up to date" % col["name"])
                        break

    def _run_mirror(self, L, folder, mode, start_date, end_date, post_filter):
        """Rebuilds the whole saved library with the collections as folders.

        Every collection is listed before anything downloads, so the posts it
        holds can be kept out of the main folder. Without that they would land
        twice, once in the folder and once loose, since Instagram counts a
        collected post as saved as well.
        """
        import instaloader
        collections = self.fetch_collections(L)
        self.emit("log", text="%d collections on Instagram" % len(collections))

        plan, held = [], set()
        for col in collections:
            if self.stop_flag.is_set():
                raise StopRequested()
            codes = list(self.collection_shortcodes(L, col["id"]))
            plan.append((col, codes))
            held.update(codes)

        seen = 0
        for col, codes in plan:
            target = safe_folder_name(col["name"])
            self.emit("log", text="Collection: %s, %d posts" % (col["name"], len(codes)))
            for code in codes:
                if self.stop_flag.is_set():
                    raise StopRequested()
                seen += 1
                self.emit("progress", done=seen, total=len(held))
                try:
                    post = instaloader.Post.from_shortcode(L.context, code)
                except Exception:
                    continue      # saved post since deleted or gone private
                if not post_filter(post):
                    continue
                L.download_post(post, target=target)

        # the rest of the library goes in the main folder, beside those folders
        L.dirname_pattern = str(folder)
        self.emit("log", text="Now the posts that are not in a collection")

        def rest_filter(post):
            if post.shortcode in held:
                return False
            return post_filter(post)

        L.download_saved_posts(fast_update=(mode == "new"), post_filter=rest_filter)

    # -- collections --------------------------------------------------------
    # Instagram serves saved collections to its own web app through
    # www.instagram.com/api/graphql. That request only answers with JSON when it
    # carries fb_dtsg, the page token every Instagram page ships. Without it the
    # site returns the app shell HTML instead, which is what earlier attempts at
    # api/v1/collections/list/ were really seeing. Both queries below are the
    # ones the browser itself sends, read off the network panel rather than
    # guessed at, and both work with the plain browser session.
    ALL_POSTS = "ALL_MEDIA_AUTO_COLLECTION"
    IG_APP_ID = "936619743392459"
    COLLECTIONS_DOC_ID = "27959320833754327"        # PolarisProfileSavedTabContentQuery_connection
    COLLECTION_MEDIA_DOC_ID = "28281867904811986"   # PolarisSavedCollectionPageWWWQuery

    def saved_page_url(self, L):
        user = L.context.username or ""
        if not user:
            return "https://www.instagram.com/"
        return "https://www.instagram.com/%s/saved/" % user

    def page_token(self, L, refresh=False):
        """Reads fb_dtsg out of a logged in page. One read lasts the session."""
        import re
        if not refresh:
            cached = getattr(self, "_fb_dtsg", "")
            if cached:
                return cached
        L.context.do_sleep()
        resp = L.context._session.get(
            self.saved_page_url(L), allow_redirects=True, timeout=30,
            headers={"Accept": "text/html,application/xhtml+xml",
                     "X-Requested-With": None, "X-Instagram-AJAX": None,
                     "Content-Length": None})
        html = resp.text or ""
        if "/accounts/login" in resp.url or "loginForm" in html:
            raise RuntimeError("Instagram redirected to the login page, so the "
                               "browser session is no longer valid. Press Switch "
                               "and connect again.")
        match = (re.search(r'"DTSGInitialData"[^}]{0,400}?"token"\s*:\s*"([^"]+)"', html)
                 or re.search(r'"dtsg"\s*:\s*\{[^}]{0,400}?"token"\s*:\s*"([^"]+)"', html))
        if not match:
            raise RuntimeError("The saved page loaded (%d KB) but carried no page "
                               "token, so the collections request cannot be signed."
                               % (len(html) // 1024))
        self._fb_dtsg = match.group(1)
        return self._fb_dtsg

    def web_graphql(self, L, doc_id, variables, friendly_name):
        """Sends one of the web app's own GraphQL queries, returns its data."""
        import json as _json
        attempt = 0
        while True:
            attempt += 1
            L.context.do_sleep()
            try:
                L.context._rate_controller.wait_before_query(doc_id)
            except Exception:
                pass
            body = {"doc_id": doc_id,
                    "fb_dtsg": self.page_token(L, refresh=attempt > 1),
                    "server_timestamps": "true",
                    "variables": _json.dumps(variables, separators=(",", ":"))}
            resp = L.context._session.post(
                "https://www.instagram.com/api/graphql", data=body,
                allow_redirects=True, timeout=60,
                headers={# these three are the whole trick. Without them
                         # Instagram reads the POST as a page visit and answers
                         # with the app shell instead of running the query
                         "Sec-Fetch-Site": "same-origin",
                         "Sec-Fetch-Mode": "cors",
                         "Sec-Fetch-Dest": "empty",
                         "X-IG-App-ID": self.IG_APP_ID,
                         "X-FB-Friendly-Name": friendly_name,
                         "X-Requested-With": "XMLHttpRequest",
                         "Accept": "*/*",
                         "Referer": self.saved_page_url(L),
                         "Origin": "https://www.instagram.com",
                         "Content-Length": None,
                         "X-Instagram-AJAX": None})
            text = resp.text or ""
            if text.startswith("for (;;);"):
                text = text[len("for (;;);"):]   # Meta's anti hijacking prefix
            try:
                payload = _json.loads(text)
            except ValueError:
                # a stale token gets the app shell back rather than an error, so
                # fetch a fresh one once before giving up
                if attempt == 1:
                    continue
                start = text.strip().replace("\n", " ")[:90]
                raise RuntimeError(
                    "Instagram answered HTTP %s with %s instead of JSON, body starts %r"
                    % (resp.status_code, resp.headers.get("content-type", "no type"),
                       start))
            if payload.get("error"):
                # the token no longer matches this session, read a fresh one
                if attempt == 1:
                    continue
                raise RuntimeError("Instagram refused the query, code %s"
                                   % payload.get("error"))
            errors = payload.get("errors")
            if errors:
                first = errors[0] if isinstance(errors, list) and errors else errors
                message = first.get("message") if isinstance(first, dict) else str(first)
                raise RuntimeError("Instagram refused the query: %s" % message)
            return payload.get("data") or {}

    def fetch_collections(self, L):
        """Every named collection on the account, as id, name and count."""
        found, after = [], None
        while True:
            variables = {"first": 30,
                         "collection_types": ["ALL_MEDIA_AUTO_COLLECTION", "MEDIA",
                                              "AUDIO_AUTO_COLLECTION"]}
            if after:
                variables["after"] = after
            data = self.web_graphql(L, self.COLLECTIONS_DOC_ID, variables,
                                    "PolarisProfileSavedTabContentQuery_connection")
            viewer = data.get("viewer")
            if not viewer:
                raise RuntimeError("Instagram did not recognise the session on the "
                                   "collections request. Press Switch and connect "
                                   "the browser again.")
            conn = viewer.get("collections_unified_with_auto_collections") or {}
            for edge in conn.get("edges") or []:
                node = edge.get("node") or {}
                cid = str(node.get("collection_id") or "")
                # MediaCollection is a real folder, the other two types are the
                # automatic All posts and Audio shelves
                if not cid or cid == self.ALL_POSTS:
                    continue
                if node.get("__typename") != "MediaCollection":
                    continue
                found.append({"id": cid,
                              "name": node.get("collection_name") or "Untitled",
                              "count": node.get("collection_media_count")})
            page = conn.get("page_info") or {}
            if not page.get("has_next_page"):
                break
            after = page.get("end_cursor")
            if not after:
                break
        return found

    def list_collections(self, username, session_file):
        def work():
            try:
                import instaloader
                L = instaloader.Instaloader(quiet=True)
                L.load_session_from_file(username, str(session_file))
                items = self.fetch_collections(L)
            except Exception as err:
                self.emit("collections_failed",
                          detail=str(err) or type(err).__name__)
                return
            self.emit("collections", items=items)
            # Instagram's count includes posts it will not serve any more, so
            # the only way to know what is still there is to ask for the listing
            # and see what comes back. One request per collection, reported as
            # each one lands so the picker fills in rather than waiting.
            for col in items:
                if self.stop_flag.is_set():
                    return
                try:
                    live = sum(1 for _ in self.collection_shortcodes(L, col["id"]))
                except Exception:
                    live = None
                self.emit("collection_counted", id=col["id"], live=live)

        threading.Thread(target=work, daemon=True).start()

    def collection_shortcodes(self, L, collection_id):
        """Yields the shortcode of every post in one collection."""
        after = None
        while True:
            variables = {"after": after, "first": 24,
                         "collection_id": str(collection_id),
                         "__relay_internal__pv__PolarisShortDramaEnabledrelayprovider":
                             False}
            data = self.web_graphql(L, self.COLLECTION_MEDIA_DOC_ID, variables,
                                    "PolarisSavedCollectionPageWWWQuery")
            media = (data.get("fetch__MediaCollection") or {}).get("media") or {}
            for edge in media.get("edges") or []:
                code = (edge.get("node") or {}).get("code")
                if code:
                    yield code
            page = media.get("page_info") or {}
            if not page.get("has_next_page"):
                break
            after = page.get("end_cursor")
            if not after:
                break

    def collection_posts(self, L, collection_id):
        """Yields Posts from one collection, in the order Instagram lists them.

        The listing carries a media dict per post but without taken_at, so it
        cannot be turned into a Post directly. It does carry the shortcode, and
        Instaloader builds a complete Post from that, which keeps dates, videos
        and carousels working exactly as they do everywhere else.
        """
        import instaloader
        for code in self.collection_shortcodes(L, collection_id):
            try:
                yield instaloader.Post.from_shortcode(L.context, code)
            except Exception:
                continue          # saved post since deleted or gone private

    # -- sleep prevention ---------------------------------------------------
    # A full run takes hours, so the machine is asked to stay awake. macOS has
    # caffeinate, Windows has an execution state flag, elsewhere it is left alone.
    def start_caffeinate(self):
        if self.caffeinate is not None:
            return
        if IS_MAC:
            try:
                self.caffeinate = subprocess.Popen(
                    ["caffeinate", "-i", "-w", str(os.getpid())],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                self.caffeinate = None
        elif IS_WINDOWS:
            try:
                import ctypes
                # ES_CONTINUOUS | ES_SYSTEM_REQUIRED, held by this thread
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
                self.caffeinate = "windows"
            except Exception:
                self.caffeinate = None

    def stop_caffeinate(self):
        if self.caffeinate is None:
            return
        if self.caffeinate == "windows":
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            except Exception:
                pass
        else:
            try:
                self.caffeinate.terminate()
            except Exception:
                pass
        self.caffeinate = None

    # -- run ----------------------------------------------------------------
    def start(self, username, session_file, folder, mode, start_date, end_date,
              collections=None, mirror=False):
        self.stop_flag.clear()
        self.thread = threading.Thread(
            target=self._run,
            args=(username, session_file, folder, mode, start_date, end_date,
                  collections, mirror),
            daemon=True)
        self.thread.start()

    def request_stop(self):
        self.stop_flag.set()

    def _run(self, username, session_file, folder, mode, start_date, end_date,
             collections=None, mirror=False):
        import instaloader
        from instaloader import exceptions as ex

        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
        logfile = SUPPORT_DIR / "log.txt"
        self.start_caffeinate()

        try:
            pattern = (str(Path(folder) / "{target}")
                       if (collections or mirror) else str(folder))
            L = instaloader.Instaloader(
                dirname_pattern=pattern,
                download_video_thumbnails=False,
                save_metadata=False,        # no .json.xz beside each post
                post_metadata_txt_pattern="",  # no .txt caption beside each post
                download_comments=False,
                download_geotags=False,
                max_connection_attempts=3,
            )
            L.load_session_from_file(username, str(session_file))

            def log_to_ui(*msg, sep="", end="\n", flush=False):
                text = sep.join(str(m) for m in msg)
                if not text.strip():
                    return
                try:
                    with open(logfile, "a") as fh:
                        fh.write(text + ("" if end == "" else "\n"))
                except Exception:
                    pass
                prog = parse_progress(text)
                if prog:
                    self.emit("progress", done=prog[0], total=prog[1])
                else:
                    self.emit("log", text=text.strip())

            L.context.log = log_to_ui
            L.context.error = lambda msg, repeat_at_end=True: self.emit("log", text=str(msg))

            matched = [0]

            def post_filter(post):
                if self.stop_flag.is_set():
                    raise StopRequested()
                if mode == "dates":
                    when = post.date_utc
                    if start_date and when < start_date:
                        return False
                    if end_date and when > end_date:
                        return False
                    matched[0] += 1
                    self.emit("matched", count=matched[0])
                return True

            if mirror:
                self._run_mirror(L, folder, mode, start_date, end_date, post_filter)
            elif collections:
                self._run_collections(L, collections, mode, start_date, end_date,
                                      post_filter)
            else:
                if mode == "dates":
                    self.emit("log",
                              text="Looking through your saved posts for that period")
                else:
                    self.emit("log", text="Asking Instagram for your saved posts")
                L.download_saved_posts(fast_update=(mode == "new"),
                                       post_filter=post_filter)
            self.emit("done")

        except StopRequested:
            self.emit("stopped")
        except Exception as err:
            name = type(err).__name__
            if name == "AbortDownloadException":
                text = str(err).lower()
                if "checkpoint" in text or "challenge" in text:
                    self.emit("blocked", reason="challenge", detail=str(err))
                else:
                    self.emit("blocked", reason="feedback", detail=str(err))
            elif name == "TooManyRequestsException":
                self.emit("blocked", reason="ratelimit", detail=str(err))
            elif name in ("LoginRequiredException", "LoginException",
                          "InvalidArgumentException"):
                self.emit("signedout", detail=str(err))
            elif name == "ConnectionException":
                self.emit("blocked", reason="network", detail=str(err))
            else:
                self.emit("failed", detail="%s: %s" % (name, err))
        finally:
            self.stop_caffeinate()


# ----------------------------------------------------------------------------
# Interface
# ----------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.engine = Engine(self.q)

        prefs = load_prefs()
        self.folder = Path(prefs.get("folder") or default_folder())
        self.completed_once = bool(prefs.get("completed_once"))
        self.mode = prefs.get("mode") or ("new" if self.completed_once else "all")
        self.date_from = prefs.get("date_from", "")
        self.date_to = prefs.get("date_to", "")
        self.range_problem = ""
        self.matched = 0
        self.collections = None       # list from Instagram, None until fetched
        self.chosen = set(prefs.get("chosen_collections") or [])
        self.collections_error = ""
        self.collections_detail = ""
        self.collection_live = {}     # collection id -> posts Instagram still serves
        self.picker = None
        self.picker_hover = None
        self.spin_frame = None      # canvas and place of the live spinner
        self.spin_job = None
        self.spin_step = 0
        self.username = None
        self.session_file = None

        self.state = "idle"          # idle | running | paused | done | error
        self.done = 0
        self.total = 0
        self.session_start_done = 0
        self.status_line = ""
        self.error_text = ""
        self.attempt = 0
        self.pause_reason = "feedback"
        self.verified = False
        self.resume_at = 0.0
        self.started_at = 0.0

        self.hover = None
        self.sheet = None
        self.sheet_busy = None
        self.sheet_error = ""
        self.sheet_hover = None

        root.title(APP_NAME)
        root.configure(bg=BG)
        self.win_h = WIN_H
        root.geometry("%dx%d" % (WIN_W, WIN_H))
        root.resizable(False, False)

        self.f_title = self.font(DISPLAY_FACES, 22, "bold")
        self.f_head = self.font(TEXT_FACES, 15, "normal")
        self.f_head_b = self.font(TEXT_FACES, 15, "bold")
        self.f_body = self.font(TEXT_FACES, 13, "normal")
        self.f_small = self.font(TEXT_FACES, 11, "normal")
        self.f_mono = self.font(["SF Mono", "Menlo", "Monaco"], 28, "normal")
        self.f_big = self.font(DISPLAY_FACES, 30, "bold")

        self.canvas = tk.Canvas(root, width=WIN_W, height=WIN_H, bg=BG,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        def make_entry(value):
            e = tk.Entry(self.canvas, bg=CARD_HI, fg=LABEL, insertbackground=LABEL,
                         relief="flat", highlightthickness=0, justify="center",
                         font=self.f_body, disabledbackground=CARD_HI)
            e.insert(0, value)
            e.bind("<KeyRelease>", self.on_date_typed)
            return e

        self.e_from = make_entry(self.date_from)
        self.e_to = make_entry(self.date_to)

        self.autodetect_session()
        self.render()
        # some Tk builds paint nothing until the window has been mapped
        self.canvas.bind("<Map>", lambda e: self.render())
        self.root.after(60, self.render)
        self.root.after(100, self.pump)
        self.root.after(1000, self.tick)

    # -- small helpers ------------------------------------------------------
    def font(self, families, size, weight):
        available = set(tkfont.families(self.root))
        family = next((f for f in families if f in available), families[-1])
        return (family, -abs(size), weight)

    def on_date_typed(self, _event=None):
        self.date_from = self.e_from.get()
        self.date_to = self.e_to.get()
        if self.range_problem:
            self.range_problem = ""
            self.render()

    def set_mode(self, mode):
        self.mode = mode
        self.range_problem = ""
        prefs = load_prefs()
        prefs["mode"] = mode
        save_prefs(prefs)
        self.render()

    def autodetect_session(self):
        sessions = find_sessions()
        if sessions:
            self.username, self.session_file = sessions[0]

    # -- drawing primitives -------------------------------------------------
    def round_rect(self, x1, y1, x2, y2, r, fill, outline="", tag=None):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        kw = dict(smooth=True, fill=fill, outline=outline or fill)
        if tag:
            kw["tags"] = tag
        return self.canvas.create_polygon(pts, **kw)

    def text(self, x, y, s, font, fill=LABEL, anchor="w", width=None, tag=None):
        kw = dict(text=s, font=font, fill=fill, anchor=anchor)
        if width:
            kw["width"] = width
        if tag:
            kw["tags"] = tag
        return self.canvas.create_text(x, y, **kw)

    def button(self, x, y, w, h, label, command, kind="secondary", enabled=True):
        tag = "btn_" + re.sub(r"\W+", "_", label.lower())
        hovered = self.hover == tag and enabled
        if kind == "primary":
            fill = (BLUE_HI if hovered else BLUE) if enabled else "#2A3A4A"
            fg = LABEL if enabled else LABEL_3
            font = self.f_head_b
        elif kind == "warn":
            fill = (CARD_HI if hovered else CARD)
            fg = ORANGE
            font = self.f_body
        else:
            fill = (CARD_HI if hovered else "#39393C")
            fg = LABEL if enabled else LABEL_3
            font = self.f_body
        self.round_rect(x, y, x + w, y + h, min(h / 2, 10), fill, tag=tag)
        self.text(x + w / 2, y + h / 2 + 1, label, font, fill=fg, anchor="center", tag=tag)
        if enabled:
            self.canvas.tag_bind(tag, "<Button-1>", lambda e, c=command: c())
            self.canvas.tag_bind(tag, "<Enter>", lambda e, t=tag: self.set_hover(t))
            self.canvas.tag_bind(tag, "<Leave>", lambda e: self.set_hover(None))

    def segmented(self, x, y, w, h, options, active, command, enabled=True):
        """Apple style segmented control. options is a list of (key, label).

        Every segment gets an opaque rounded rect carrying the tag, because a Tk
        canvas item with no fill is only clickable along its outline.
        """
        TRACK, PILL = "#2C2C2E", "#4A4A4E"
        self.round_rect(x, y, x + w, y + h, 9, TRACK)
        seg = w / len(options)
        for i, (key, label) in enumerate(options):
            sx = x + i * seg
            tag = "seg_" + key
            selected = key == active
            hovered = self.hover == tag and enabled and not selected
            fill = PILL if selected else ("#37373A" if hovered else TRACK)
            self.round_rect(sx + 2, y + 2, sx + seg - 2, y + h - 2, 7, fill, tag=tag)
            if enabled:
                fg = LABEL if selected else LABEL_2
            else:
                fg = LABEL_3
            self.text(sx + seg / 2, y + h / 2 + 1, label,
                      self.f_head_b if selected else self.f_body,
                      fill=fg, anchor="center", tag=tag)
            if enabled:
                self.canvas.tag_bind(tag, "<Button-1>", lambda e, k=key: command(k))
                self.canvas.tag_bind(tag, "<Enter>", lambda e, t=tag: self.set_hover(t))
                self.canvas.tag_bind(tag, "<Leave>", lambda e: self.set_hover(None))

    def set_hover(self, tag):
        if self.hover != tag:
            self.hover = tag
            self.canvas.configure(cursor=HAND if tag else "")
            self.render()

    def section_label(self, y, s):
        self.text(MARGIN + 4, y, s.upper(), self.f_small, fill=LABEL_2)

    # -- layout -------------------------------------------------------------
    def render(self):
        self.canvas.delete("all")
        c = self.canvas
        settled = self.state in ("idle", "done", "error")

        self.text(MARGIN, 38, "Instagram Saved", self.f_title)
        self.text(MARGIN, 56, "Keep a copy of every post you bookmarked, "
                              "before it disappears.", self.f_body, fill=LABEL_2,
                  anchor="nw", width=CONTENT_W)

        y = 104
        self.section_label(y, "Account")
        y += 14
        self.round_rect(MARGIN, y, MARGIN + CONTENT_W, y + 64, 12, CARD)
        if self.username:
            c.create_oval(MARGIN + 20, y + 28, MARGIN + 28, y + 36, fill=GREEN, outline=GREEN)
            self.text(MARGIN + 38, y + 24, "@" + self.username, self.f_head)
            self.text(MARGIN + 38, y + 43,
                      "Connected" if self.verified else "Saved login found",
                      self.f_small, fill=LABEL_2)
            if settled:
                self.button(MARGIN + CONTENT_W - 108, y + 17, 92, 30,
                            "Switch", self.open_connect_sheet)
        else:
            c.create_oval(MARGIN + 20, y + 28, MARGIN + 28, y + 36,
                          fill=LABEL_3, outline=LABEL_3)
            self.text(MARGIN + 38, y + 24, "Not connected", self.f_head)
            self.text(MARGIN + 38, y + 43, "Uses the login already in your browser",
                      self.f_small, fill=LABEL_2)
            self.button(MARGIN + CONTENT_W - 108, y + 17, 92, 30,
                        "Connect", self.open_connect_sheet, kind="primary")
        y += 64 + 18

        self.section_label(y, "Save to")
        y += 14
        self.round_rect(MARGIN, y, MARGIN + CONTENT_W, y + 64, 12, CARD)
        self.text(MARGIN + 20, y + 24, shorten_path(self.folder), self.f_head)
        self.text(MARGIN + 20, y + 43, self.folder_hint(), self.f_small, fill=LABEL_2)
        if settled:
            self.button(MARGIN + CONTENT_W - 108, y + 17, 92, 30, "Change", self.choose_folder)
        else:
            self.button(MARGIN + CONTENT_W - 108, y + 17, 92, 30, "Reveal", self.reveal)
        y += 64 + 18

        self.section_label(y, "Collections")
        y += 14
        self.round_rect(MARGIN, y, MARGIN + CONTENT_W, y + 64, 12, CARD)
        self.text(MARGIN + 20, y + 24, self.chosen_title(), self.f_head)
        self.text(MARGIN + 20, y + 43, self.chosen_hint(), self.f_small, fill=LABEL_2)
        if settled:
            self.button(MARGIN + CONTENT_W - 108, y + 17, 92, 30,
                        "Choose", self.open_picker)
        y += 64 + 18

        self.section_label(y, "What to fetch")
        y += 16
        self.segmented(MARGIN, y, CONTENT_W, 34,
                       [("all", "Everything"), ("new", "Only new"),
                        ("dates", "Date range")],
                       self.mode, self.set_mode, enabled=settled)
        y += 34

        if self.mode == "dates":
            y += 10
            self.round_rect(MARGIN, y, MARGIN + CONTENT_W, y + 56, 12, CARD)
            self.text(MARGIN + 20, y + 28, "From", self.f_body, fill=LABEL_2)
            self.text(MARGIN + 250, y + 28, "To", self.f_body, fill=LABEL_2)
            c.create_window(MARGIN + 60, y + 28, window=self.e_from,
                            anchor="w", width=160, height=26)
            c.create_window(MARGIN + 280, y + 28, window=self.e_to,
                            anchor="w", width=160, height=26)
            for e in (self.e_from, self.e_to):
                e.configure(state="normal" if settled else "disabled")
            y += 56
            hint = self.range_problem or "Leave one side empty for open ended, " \
                                         "any of 2024, 2024-06 or 15/06/2024 works"
            self.text(MARGIN + 4, y + 14, hint, self.f_small,
                      fill=ORANGE if self.range_problem else LABEL_3)
            y += 24

        y += 18

        if settled:
            self.button(MARGIN, y, CONTENT_W, 44, self.start_label(), self.start,
                        kind="primary", enabled=bool(self.username))
        else:
            self.button(MARGIN, y, CONTENT_W, 44, "Stop", self.stop)
        y += 44 + 18

        panel = {
            "idle": self.draw_idle,
            "running": self.draw_running,
            "paused": self.draw_paused,
            "done": self.draw_done,
            "error": self.draw_error,
        }[self.state]
        y += panel(y) or 0

        height = int(y + 52)
        self.text(WIN_W / 2, height - 26,
                  "Downloads run through Instaloader. Your password stays with Instagram.",
                  self.f_small, fill=LABEL_3, anchor="center", width=CONTENT_W)
        self.resize(height)

    def resize(self, height):
        height = max(420, min(height, 900))
        if abs(height - self.win_h) > 2:
            self.win_h = height
            self.canvas.configure(height=height)
            self.root.geometry("%dx%d" % (WIN_W, height))

    def start_label(self):
        if self.mode == "new":
            return "Check for new posts"
        if self.mode == "dates":
            return "Fetch that period"
        return "Start archiving"

    def chosen_title(self):
        if not self.chosen:
            return "Everything you have saved"
        if MIRROR in self.chosen:
            return "Everything, in collection folders"
        if len(self.chosen) == 1:
            name = next((c["name"] for c in (self.collections or [])
                         if c["id"] in self.chosen), None)
            return name or "1 collection"
        return "%d collections" % len(self.chosen)

    def chosen_hint(self):
        if self.collections_error:
            return "Collection list did not load"
        if not self.chosen:
            return "One flat folder, the way Instagram's All Posts works"
        if MIRROR in self.chosen:
            return "Every collection becomes a folder, the rest sits beside them"
        return "Each one becomes its own folder"

    def folder_hint(self):
        try:
            n = len([f for f in self.folder.iterdir()
                     if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".mp4", ".webp")])
            return "%s files already saved" % format_count(n) if n else "Empty folder"
        except Exception:
            return "Will be created when you start"

    IDLE_TEXT = {
        "new": "Fetches only what you have saved since the last complete run, then "
               "stops. Quick, and the usual choice for a top up.",
        "dates": "Reads through your saved list and downloads only the posts "
                 "published inside that period. Anything already in the folder is "
                 "left alone, so running it twice costs nothing.",
        "all": "The first run can take a few evenings. Instagram limits how fast an "
               "account can pull media, so the app pauses when asked to and picks up "
               "where it stopped. Leave it open and the lid up.",
    }

    def draw_idle(self, y):
        item = self.text(MARGIN + 4, y + 4, self.IDLE_TEXT[self.mode], self.f_body,
                         fill=LABEL_2, anchor="nw", width=CONTENT_W - 8)
        return self.canvas.bbox(item)[3] - y

    def draw_running(self, y):
        self.round_rect(MARGIN, y, MARGIN + CONTENT_W, y + 116, 12, CARD)
        pct = (self.done / self.total) if self.total else 0
        big = self.text(MARGIN + 20, y + 30, format_count(self.done), self.f_big)
        x_after = self.canvas.bbox(big)[2] + 10
        if self.mode == "dates":
            self.text(x_after, y + 38,
                      "of %s checked, %s in range" % (format_count(self.total or 0),
                                                      format_count(self.matched)),
                      self.f_body, fill=LABEL_2)
        else:
            self.text(x_after, y + 38, "of %s saved posts" % format_count(self.total or 0),
                      self.f_body, fill=LABEL_2)

        bx1, bx2, by = MARGIN + 20, MARGIN + CONTENT_W - 20, y + 62
        self.round_rect(bx1, by, bx2, by + 6, 3, FILL_TRACK)
        if pct > 0:
            self.round_rect(bx1, by, bx1 + max(6, (bx2 - bx1) * pct), by + 6, 3, BLUE)

        self.text(MARGIN + 20, y + 90, self.status_line or "Working", self.f_small,
                  fill=LABEL_2, width=CONTENT_W - 140)
        self.text(MARGIN + CONTENT_W - 20, y + 90, self.eta_text(), self.f_small,
                  fill=LABEL_3, anchor="e")
        return 116

    def eta_text(self):
        gained = self.done - self.session_start_done
        elapsed = time.time() - self.started_at
        if gained < 15 or elapsed < 30 or not self.total:
            return ""
        rate = gained / elapsed
        left = (self.total - self.done) / rate if rate else 0
        if left > 86400 * 2:
            return ""
        hours = left / 3600
        return "about %.0f h of downloading left" % hours if hours >= 1 else "almost there"

    def draw_paused(self, y):
        body = self.pause_body()
        probe = self.text(-9999, -9999, body, self.f_body, width=CONTENT_W - 40)
        box = self.canvas.bbox(probe)
        self.canvas.delete(probe)
        h = max(160, (box[3] - box[1]) + 122)
        self.round_rect(MARGIN, y, MARGIN + CONTENT_W, y + h, 12, ORANGE_DIM)
        self.text(MARGIN + 20, y + 26, self.pause_title(), self.f_head_b, fill=ORANGE)
        self.text(MARGIN + 20, y + 44, body, self.f_body, fill="#E8C89A",
                  anchor="nw", width=CONTENT_W - 40)

        remaining = max(0, self.resume_at - time.time())
        self.text(MARGIN + 20, y + h - 34, format_countdown(remaining), self.f_mono,
                  fill=LABEL, anchor="w")
        self.text(MARGIN + 20 + 96, y + h - 28, "until it tries again", self.f_small,
                  fill=LABEL_2)
        self.button(MARGIN + CONTENT_W - 124, y + h - 50, 108, 32, "Try now",
                    self.resume_now, kind="warn")
        return h

    def pause_title(self):
        return {
            "challenge": "Instagram wants you to confirm it is you",
            "ratelimit": "Too many requests for now",
            "network": "Lost the connection",
        }.get(self.pause_reason, "Instagram paused the download")

    def pause_body(self):
        saved = "%s posts are already saved and safe." % format_count(self.done)
        if self.pause_reason == "challenge":
            return ("Open Instagram on your phone and approve the login notice, "
                    "then press Try now. " + saved)
        if self.pause_reason == "network":
            return "The Mac lost its connection to Instagram. " + saved
        return ("This is normal on a big archive. Instagram limits how much an "
                "account can download at once and lifts it on its own, usually "
                "within a few hours. Nothing was lost. " + saved)

    def draw_done(self, y):
        if self.mode == "dates":
            title = "Period fetched"
            body = ("%s posts fell inside that range and are in your folder."
                    % format_count(self.matched))
        elif self.mode == "new":
            title = "Up to date"
            body = "Nothing new left to fetch."
        else:
            title = "Archive complete"
            body = ("%s posts are in your folder. Run it again any time to pick up "
                    "new saves." % format_count(self.done))
        probe = self.text(-9999, -9999, body, self.f_body, width=CONTENT_W - 40)
        h = max(92, (self.canvas.bbox(probe)[3] - self.canvas.bbox(probe)[1]) + 74)
        self.canvas.delete(probe)
        self.round_rect(MARGIN, y, MARGIN + CONTENT_W, y + h, 12, CARD)
        self.text(MARGIN + 20, y + 30, title, self.f_head_b, fill=GREEN)
        item = self.text(MARGIN + 20, y + 52, body,
                         self.f_body, fill=LABEL_2, anchor="nw", width=CONTENT_W - 40)
        return h

    def draw_error(self, y):
        probe = self.text(-9999, -9999, self.error_text, self.f_body, width=CONTENT_W - 40)
        h = max(100, (self.canvas.bbox(probe)[3] - self.canvas.bbox(probe)[1]) + 70)
        self.canvas.delete(probe)
        self.round_rect(MARGIN, y, MARGIN + CONTENT_W, y + h, 12, CARD)
        self.text(MARGIN + 20, y + 26, "Something went wrong", self.f_head_b, fill=RED)
        item = self.text(MARGIN + 20, y + 48, self.error_text, self.f_body, fill=LABEL_2,
                         anchor="nw", width=CONTENT_W - 40)
        return h

    # -- connect sheet ------------------------------------------------------
    SHEET_W, SHEET_H = 380, 372

    def open_connect_sheet(self):
        if getattr(self, "sheet", None) is not None:
            try:
                self.sheet.lift()
                return
            except tk.TclError:
                self.sheet = None

        sheet = tk.Toplevel(self.root)
        sheet.title("Connect")
        sheet.configure(bg=BG)
        sheet.resizable(False, False)
        sheet.transient(self.root)
        x = self.root.winfo_x() + (WIN_W - self.SHEET_W) // 2
        y = self.root.winfo_y() + 120
        sheet.geometry("%dx%d+%d+%d" % (self.SHEET_W, self.SHEET_H, x, y))

        self.sheet = sheet
        self.sheet_canvas = tk.Canvas(sheet, width=self.SHEET_W, height=self.SHEET_H,
                                      bg=BG, highlightthickness=0, bd=0)
        self.sheet_canvas.pack()
        self.sheet_busy = None
        self.sheet_error = ""
        self.sheet_hover = None
        sheet.bind("<Destroy>", self.on_sheet_closed)
        self.render_sheet()

    def on_sheet_closed(self, event=None):
        if event is None or event.widget is self.sheet:
            self.sheet = None

    def sheet_hover_set(self, tag):
        if self.sheet_hover != tag:
            self.sheet_hover = tag
            self.render_sheet()

    def render_sheet(self):
        """Redrawn whole rather than recoloured in place.

        Recolouring by tag repainted the label the same shade as the button under
        it, which made the text vanish on hover.
        """
        if self.sheet is None:
            return
        cv = self.sheet_canvas
        cv.delete("all")
        busy = self.sheet_busy is not None
        if not busy:
            self.spinner_off(cv)

        cv.create_text(24, 32, text="Where are you signed in?", anchor="w",
                       font=self.f_head_b, fill=LABEL)
        cv.create_text(24, 52, anchor="nw", width=self.SHEET_W - 48,
                       text="InstaSaver borrows the Instagram session from your "
                            "browser. It never sees your password.",
                       font=self.f_small, fill=LABEL_2)

        for i, name in enumerate(BROWSERS[:6]):
            col, row = i % 2, i // 2
            bx, by = 24 + col * 172, 106 + row * 48
            w, h = 160, 38
            tag = "sheet_" + name
            active = self.sheet_busy == name
            hovered = self.sheet_hover == tag and not busy
            if active:
                fill, fg = BLUE, LABEL
            elif busy:
                fill, fg = "#252527", LABEL_3
            else:
                fill, fg = (CARD_HI if hovered else CARD), LABEL
            r = 9
            pts = [bx + r, by, bx + w - r, by, bx + w, by, bx + w, by + r,
                   bx + w, by + h - r, bx + w, by + h, bx + w - r, by + h,
                   bx + r, by + h, bx, by + h, bx, by + h - r, bx, by + r, bx, by]
            cv.create_polygon(pts, smooth=True, fill=fill, outline=fill, tags=tag)
            # the label carries no tag, so nothing can recolour it by accident
            cv.create_text(bx + w / 2 + (11 if active else 0), by + h / 2 + 1,
                           text=("Connecting" if active else name),
                           font=self.f_body, fill=fg)
            if active:
                self.spinner_at(cv, bx + 34, by + h / 2 + 1, r=7, over=BLUE,
                                tint=LABEL)
            if not busy:
                cv.tag_bind(tag, "<Button-1>", lambda e, n=name: self.pick_browser(n))
                cv.tag_bind(tag, "<Enter>", lambda e, t=tag: self.sheet_hover_set(t))
                cv.tag_bind(tag, "<Leave>", lambda e: self.sheet_hover_set(None))

        if busy:
            message, colour = ("Asking %s for the session. macOS may ask permission "
                               "to read its saved data, press Allow."
                               % self.sheet_busy), LABEL_2
        elif self.sheet_error:
            message, colour = self.sheet_error, ORANGE
        else:
            message, colour = ("Pick the browser where instagram.com is already "
                               "logged in."), LABEL_3
        cv.create_text(24, 268, text=message, anchor="nw",
                       width=self.SHEET_W - 48, font=self.f_small, fill=colour)

    def pick_browser(self, name):
        if self.sheet_busy is not None:
            return
        self.sheet_busy = name
        self.sheet_error = ""
        self.sheet_hover = None
        self.render_sheet()

        def work():
            try:
                who = self.engine.connect_browser(name)
            except Exception as err:
                self.q.put(dict(kind="connect_failed",
                                detail=friendly_connect_error(err, name)))
            else:
                self.q.put(dict(kind="connected", who=who))

        threading.Thread(target=work, daemon=True).start()

    # -- collections picker -------------------------------------------------
    PICK_W, PICK_H = 400, 580
    ROWS_VISIBLE = 7

    def open_picker(self):
        if not self.username:
            return
        if self.picker is not None:
            try:
                self.picker.lift()
                return
            except tk.TclError:
                self.picker = None

        win = tk.Toplevel(self.root)
        win.title("Collections")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.transient(self.root)
        win.geometry("%dx%d+%d+%d" % (self.PICK_W, self.PICK_H,
                                      self.root.winfo_x() + (WIN_W - self.PICK_W) // 2,
                                      self.root.winfo_y() + 90))
        self.picker = win
        self.picker_canvas = tk.Canvas(win, width=self.PICK_W, height=self.PICK_H,
                                       bg=BG, highlightthickness=0, bd=0)
        self.picker_canvas.pack()
        self.picker_scroll = 0
        self.picker_hover = None
        win.bind("<Destroy>", self.on_picker_closed)
        self.picker_canvas.bind("<MouseWheel>", self.picker_wheel)
        self.render_picker()

        if self.collections is None:
            self.collections_error = ""
            self.collections_detail = ""
            self.engine.list_collections(self.username, self.session_file)

    def on_picker_closed(self, event=None):
        if event is None or event.widget is self.picker:
            self.spinner_off(self.picker_canvas)
            self.picker = None

    def picker_wheel(self, event):
        if not self.collections:
            return
        rows = len(self.collections) + 2
        limit = max(0, rows - self.ROWS_VISIBLE)
        self.picker_scroll = max(0, min(limit, self.picker_scroll - event.delta))
        self.render_picker()

    def picker_hover_set(self, tag):
        if self.picker_hover != tag:
            self.picker_hover = tag
            self.render_picker()

    def render_picker(self):
        if self.picker is None:
            return
        cv = self.picker_canvas
        cv.delete("all")
        W = self.PICK_W

        cv.create_text(24, 30, text="Your Instagram collections", anchor="w",
                       font=self.f_head_b, fill=LABEL)
        cv.create_text(24, 50, anchor="nw", width=W - 48, font=self.f_small,
                       fill=LABEL_2,
                       text="Pick the ones to download. Each becomes a folder inside "
                            "your save location.")

        top = 96
        row_h = 52

        if self.collections_error:
            self.spinner_off(cv)
            item = cv.create_text(24, top, anchor="nw", width=W - 48, font=self.f_body,
                                  fill=ORANGE, text=self.collections_error)
            ry = cv.bbox(item)[3] + 14
            if self.collections_detail:
                detail = cv.create_text(24, ry, anchor="nw", width=W - 48,
                                        font=self.f_small, fill=LABEL_3,
                                        text="What Instagram said: "
                                             + self.collections_detail)
                ry = cv.bbox(detail)[3] + 16
            tag = "picker_retry"
            self.round_rect_on(cv, 24, ry, 140, ry + 32, 9,
                               CARD_HI if self.picker_hover == tag else CARD, tag=tag)
            cv.create_text(82, ry + 16, text="Try again", font=self.f_body,
                           fill=LABEL, anchor="center", tags=tag)
            cv.tag_bind(tag, "<Button-1>", lambda e: self.retry_collections())
            cv.tag_bind(tag, "<Enter>", lambda e, t=tag: self.picker_hover_set(t))
            cv.tag_bind(tag, "<Leave>", lambda e: self.picker_hover_set(None))
        elif self.collections is None:
            self.spinner_at(cv, 33, top + 18)
            cv.create_text(54, top + 18, anchor="w", font=self.f_body, fill=LABEL_2,
                           text="Asking Instagram for your collections")
        elif not self.collections:
            self.spinner_off(cv)
            cv.create_text(24, top + 10, anchor="nw", width=W - 48, font=self.f_body,
                           fill=LABEL_2,
                           text="You have no named collections on Instagram, only the "
                                "main saved list. Leave this as Everything.")
        else:
            self.spinner_off(cv)
            rows = [{"id": None, "name": "Everything you have saved", "count": None},
                    {"id": MIRROR, "name": "Everything, in collection folders",
                     "count": None}]
            rows += self.collections
            window = rows[self.picker_scroll:self.picker_scroll + self.ROWS_VISIBLE]
            for i, row in enumerate(window):
                ry = top + i * row_h
                tag = "pick_%s" % (row["id"] or "all")
                picked = (not self.chosen) if row["id"] is None \
                    else row["id"] in self.chosen
                hovered = self.picker_hover == tag
                fill = CARD_HI if hovered else (CARD if picked else BG)
                self.round_rect_on(cv, 20, ry, W - 20, ry + row_h - 6, 8, fill, tag=tag)
                # the labels carry the row tag too, otherwise a click that lands
                # on the text itself falls on an item with no binding and nothing
                # happens, which reads as a dead row
                cv.create_text(38, ry + 22, text="✓" if picked else "",
                               font=self.f_head_b, fill=BLUE, anchor="w", tags=tag)
                cv.create_text(62, ry + 16, text=row["name"], anchor="w",
                               font=self.f_body, fill=LABEL, tags=tag)
                cv.create_text(62, ry + 33, text=self.collection_note(row), anchor="w",
                               font=self.f_small, fill=LABEL_3, tags=tag)
                if row["count"] is not None:
                    cv.create_text(W - 36, ry + 22,
                                   text=format_count(row["count"]), anchor="e",
                                   font=self.f_small, fill=LABEL_3, tags=tag)
                cv.tag_bind(tag, "<Button-1>", lambda e, r=row: self.toggle_collection(r))
                cv.tag_bind(tag, "<Enter>", lambda e, t=tag: self.picker_hover_set(t))
                cv.tag_bind(tag, "<Leave>", lambda e: self.picker_hover_set(None))

            if len(rows) > self.ROWS_VISIBLE:
                cv.create_text(W / 2, top + self.ROWS_VISIBLE * row_h + 6,
                               text="scroll for more", font=self.f_small,
                               fill=LABEL_3, anchor="center")

            summary = self.collections_summary()
            if summary:
                cv.create_text(24, self.PICK_H - 84, anchor="nw", width=W - 48,
                               font=self.f_small, fill=LABEL_2, text=summary)

        tag = "picker_done"
        self.round_rect_on(cv, 24, self.PICK_H - 60, W - 24, self.PICK_H - 20, 10,
                           BLUE_HI if self.picker_hover == tag else BLUE, tag=tag)
        cv.create_text(W / 2, self.PICK_H - 40, text="Done", font=self.f_head_b,
                       fill=LABEL, anchor="center", tags=tag)
        cv.tag_bind(tag, "<Button-1>", lambda e: self.close_picker())
        cv.tag_bind(tag, "<Enter>", lambda e, t=tag: self.picker_hover_set(t))
        cv.tag_bind(tag, "<Leave>", lambda e: self.picker_hover_set(None))

    def collection_note(self, row):
        """The line under a collection name.

        Instagram hands over two numbers, what a collection holds and what it
        will actually serve. The gap is posts that are still saved but gone,
        deleted or archived by whoever posted them. Those are the ones only a
        copy can bring back, so the picker says so plainly.
        """
        if row["id"] is None:
            return "One flat folder, everything together"
        if row["id"] == MIRROR:
            return "Collections as folders, the rest beside them"
        if row["id"] not in self.collection_live:
            return "Counting what is still there"
        live = self.collection_live[row["id"]]
        if live is None:
            return "Could not check what is still there"
        total = row.get("count")
        if total is None or live >= total:
            return "%s recoverable" % format_count(live)
        return "%s recoverable, %s deleted or archived" % (
            format_count(live), format_count(total - live))

    def collections_summary(self):
        """One line for the lot, only once every collection has been checked."""
        if not self.collections:
            return ""
        live = gone = 0
        for col in self.collections:
            got = self.collection_live.get(col["id"], "waiting")
            if got == "waiting" or got is None:
                return ""
            live += got
            gone += max(0, (col.get("count") or got) - got)
        if not gone:
            return "Across %d collections, all %s posts are still there" % (
                len(self.collections), format_count(live))
        return "Across %d collections, %s recoverable and %s deleted or archived" % (
            len(self.collections), format_count(live), format_count(gone))

    def retry_collections(self):
        self.collections = None
        self.collections_error = ""
        self.collections_detail = ""
        self.collection_live = {}
        self.picker_hover = None
        self.render_picker()
        self.engine.list_collections(self.username, self.session_file)

    # -- spinner ------------------------------------------------------------
    # Twelve spokes turning clockwise, the way macOS draws one. A canvas has no
    # alpha, so each spoke is mixed into the colour behind it by hand.
    SPIN_TICKS = 12
    SPIN_MS = 80

    def spinner_at(self, cv, cx, cy, r=9, over=BG, tint=LABEL_2):
        """Draws a spinner and keeps it turning. Safe to call on every render."""
        self.spin_frame = (cv, cx, cy, r, over, tint)
        self.draw_spinner()
        if self.spin_job is None:
            self.spin_job = self.root.after(self.SPIN_MS, self.spin_tick)

    def draw_spinner(self):
        if self.spin_frame is None:
            return
        cv, cx, cy, r, over, tint = self.spin_frame
        cv.delete("spinner")
        back = [int(over[i:i + 2], 16) for i in (1, 3, 5)]
        front = [int(tint[i:i + 2], 16) for i in (1, 3, 5)]
        for i in range(self.SPIN_TICKS):
            trail = (self.spin_step - i) % self.SPIN_TICKS
            fade = 0.18 + 0.82 * (1 - trail / float(self.SPIN_TICKS))
            colour = "#%02x%02x%02x" % tuple(
                int(b + (f - b) * fade) for f, b in zip(front, back))
            angle = math.radians(i * (360.0 / self.SPIN_TICKS) - 90)
            cv.create_line(cx + math.cos(angle) * r * 0.52,
                           cy + math.sin(angle) * r * 0.52,
                           cx + math.cos(angle) * r, cy + math.sin(angle) * r,
                           fill=colour, width=2, capstyle="round", tags="spinner")

    def spin_tick(self):
        self.spin_job = None
        if self.spin_frame is None:
            return
        self.spin_step = (self.spin_step + 1) % self.SPIN_TICKS
        try:
            self.draw_spinner()
        except tk.TclError:      # the window went away mid turn
            self.spin_frame = None
            return
        self.spin_job = self.root.after(self.SPIN_MS, self.spin_tick)

    def spinner_off(self, cv=None):
        """Stops the spinner, or only the one living on the given canvas."""
        if cv is not None and self.spin_frame is not None \
                and self.spin_frame[0] is not cv:
            return
        self.spin_frame = None
        if self.spin_job is not None:
            try:
                self.root.after_cancel(self.spin_job)
            except Exception:
                pass
            self.spin_job = None

    def round_rect_on(self, cv, x1, y1, x2, y2, r, fill, tag=None):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        kw = dict(smooth=True, fill=fill, outline=fill)
        if tag:
            kw["tags"] = tag
        return cv.create_polygon(pts, **kw)

    def toggle_collection(self, row):
        if row["id"] is None:
            self.chosen = set()
        elif row["id"] == MIRROR:
            # the mirror already covers every collection, so it stands alone
            self.chosen = set() if MIRROR in self.chosen else {MIRROR}
        elif row["id"] in self.chosen:
            self.chosen.discard(row["id"])
        else:
            self.chosen.discard(MIRROR)
            self.chosen.add(row["id"])
        prefs = load_prefs()
        prefs["chosen_collections"] = sorted(self.chosen)
        save_prefs(prefs)
        self.render_picker()
        self.render()

    def close_picker(self):
        if self.picker is not None:
            try:
                self.picker.destroy()
            except tk.TclError:
                pass
            self.picker = None
        self.render()

    # -- actions ------------------------------------------------------------
    def choose_folder(self):
        picked = filedialog.askdirectory(initialdir=str(self.folder.parent),
                                         title="Choose where to save")
        if picked:
            self.folder = Path(picked)
            prefs = load_prefs()
            prefs["folder"] = str(self.folder)
            save_prefs(prefs)
            self.render()

    def reveal(self):
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            if IS_WINDOWS:
                os.startfile(str(self.folder))          # noqa, Windows only
            elif IS_MAC:
                subprocess.run(["open", str(self.folder)])
            else:
                subprocess.run(["xdg-open", str(self.folder)])
        except Exception:
            pass

    def start(self):
        if not self.username:
            return

        start_date = end_date = None
        if self.mode == "dates":
            self.date_from, self.date_to = self.e_from.get(), self.e_to.get()
            start_date, end_date, problem = check_range(self.date_from, self.date_to)
            if problem:
                self.range_problem = problem
                self.render()
                return
            self.range_problem = ""
            prefs = load_prefs()
            prefs.update(date_from=self.date_from, date_to=self.date_to)
            save_prefs(prefs)

        self.matched = 0
        self.state = "running"
        self.status_line = "Asking Instagram for your saved posts"
        self.session_start_done = self.done
        self.started_at = time.time()
        picked = None
        mirror = MIRROR in self.chosen
        if not mirror and self.chosen and self.collections:
            picked = [c for c in self.collections if c["id"] in self.chosen]
        self.engine.start(self.username, self.session_file, self.folder,
                          self.mode, start_date, end_date, picked, mirror)
        self.render()

    def stop(self):
        self.engine.request_stop()
        self.status_line = "Finishing the current post, then stopping"
        if self.state == "paused":
            self.state = "idle"
        self.render()

    def resume_now(self):
        self.attempt = max(0, self.attempt - 1)
        self.start()

    # -- event loop ---------------------------------------------------------
    def tick(self):
        if self.state == "paused":
            if time.time() >= self.resume_at:
                self.start()
            else:
                self.render()
        self.root.after(1000, self.tick)

    def pump(self):
        try:
            while True:
                msg = self.q.get_nowait()
                self.handle(msg)
        except queue.Empty:
            pass
        self.root.after(120, self.pump)

    def handle(self, msg):
        kind = msg["kind"]

        if kind == "progress":
            self.done, self.total = msg["done"], msg["total"]
            self.verified = True
            if self.done - self.session_start_done > 40:
                self.attempt = 0          # real progress resets the backoff
            self.state = "running"
        elif kind == "matched":
            self.matched = msg["count"]
        elif kind == "log":
            self.status_line = msg["text"][:90]
        elif kind == "blocked":
            self.pause_reason = msg["reason"]
            self.attempt += 1
            wait = backoff_for(self.attempt, rate_limited=(msg["reason"] == "ratelimit"))
            if msg["reason"] == "network":
                wait = 120
            self.resume_at = time.time() + wait
            self.state = "paused"
        elif kind == "done":
            self.state = "done"
            prefs = load_prefs()
            prefs["folder"] = str(self.folder)
            if self.mode == "all" and not self.chosen:
                # only a full pass over everything proves the archive is complete
                self.completed_once = True
                prefs["completed_once"] = True
            save_prefs(prefs)
        elif kind == "stopped":
            self.state = "idle"
            self.status_line = ""
        elif kind == "signedout":
            self.username = None
            self.session_file = None
            self.state = "error"
            self.error_text = ("The Instagram session expired. Connect again "
                               "from your browser to carry on.")
        elif kind == "failed":
            self.state = "error"
            self.error_text = msg["detail"][:220]
        elif kind == "connected":
            self.username = msg["who"]
            self.verified = True
            self.sheet_busy = None
            if self.sheet is not None:
                try:
                    self.sheet.destroy()
                except tk.TclError:
                    pass
                self.sheet = None
            sessions = dict((u, f) for u, f in find_sessions())
            self.session_file = sessions.get(msg["who"])
            self.state = "idle"
        elif kind == "collection_counted":
            self.collection_live[msg["id"]] = msg["live"]
            self.render_picker()
            return
        elif kind == "collections":
            self.collections = msg["items"]
            self.collections_error = ""
            self.collection_live = {}
            known = {c["id"] for c in self.collections} | {MIRROR}
            self.chosen &= known          # drop collections deleted on Instagram
            self.render_picker()
            self.render()
            return
        elif kind == "collections_failed":
            self.collections_error = (
                "Could not read your collections yet. The detail below says exactly "
                "what came back. Everything you have saved downloads normally in the "
                "meantime.")
            self.collections_detail = msg["detail"][:300]
            self.render_picker()
            return
        elif kind == "connect_failed":
            self.sheet_busy = None
            self.sheet_error = msg["detail"]
            self.render_sheet()
            return

        self.render()


def main():
    if tk is None:
        print("Tkinter is missing from this Python. Install Python from python.org "
              "or run: brew install python-tk")
        sys.exit(1)
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # crisp on any display
        except Exception:
            pass
    try:
        import instaloader  # noqa: F401
        import browser_cookie3  # noqa: F401
    except ImportError as err:
        print("Missing dependency: %s\nRun: pip3 install instaloader browser_cookie3" % err)
        sys.exit(1)

    root = tk.Tk()
    if IS_MAC and float(".".join(str(tk.TkVersion).split(".")[:2])) < 8.6:
        root.withdraw()
        subprocess.run([
            "osascript", "-e",
            'display dialog "InstaSaver needs a newer Python than the one built into '
            'macOS. The built-in one uses a graphics library that draws an empty '
            'window.\n\nDownload Python from python.org, run the installer, then open '
            'InstaSaver again." buttons {"OK"} with title "InstaSaver"'
        ], capture_output=True)
        sys.exit(1)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
