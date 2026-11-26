"""Tool catalogs: Wright searches curated public lists (The Book of Secret Knowledge first) when a job
needs a tool or command it doesn't know. Entries are leads, kept in a local copy that refreshes weekly
and works offline; chapters about attacking systems are left out."""
import json

from agentkit import agent_config
from wright.agent import Agent
from wright.catalogs import CATALOGS, REFRESH_SECONDS, Catalogs, parse
from wright.llm import ScriptedModel, call, say
from wright.web import Page

BOOK = """# The Book of Secret Knowledge

#### CLI Tools &nbsp;[<sup>[TOC]</sup>](#anger-table-of-contents)

##### :black_small_square: Files and directories

<p>
&nbsp;&nbsp; <a href="https://dev.yorhel.nl/ncdu"><b>ncdu</b></a> - is an easy to use, fast disk usage analyzer.<br>
&nbsp;&nbsp; <a href="https://github.com/pkolaczk/fclones"><b>fclones</b></a> - efficient duplicate file finder and remover.<br>
</p>

##### :black_small_square: Network

<p>
&nbsp;&nbsp; <a href="https://nmap.org/"><b>nmap</b></a> - is a free and open source utility for network discovery.<br>
&nbsp;&nbsp; <a href="https://www.wireshark.org/docs/man-pages/tshark.html"><b>tshark</b></a> - dump and analyze network traffic.<br>
</p>

#### Hacking/Penetration Testing &nbsp;[<sup>[TOC]</sup>](#anger-table-of-contents)

##### :black_small_square: Pentesters arsenal tools

<p>
&nbsp;&nbsp; <a href="https://www.metasploit.com/"><b>Metasploit</b></a> - tool and framework for pentesting system.<br>
</p>

#### Other Cheat Sheets &nbsp;[<sup>[TOC]</sup>](#anger-table-of-contents)

- [Awesome Home Assistant](https://github.com/frenck/awesome-home-assistant) - a list of Home Assistant resources.
- [WAF bypass list](https://example.org/waf) - ways to bypass a web application firewall.

#### Shell One-liners &nbsp;[<sup>[TOC]</sup>](#anger-table-of-contents)

##### Tool: [find](https://en.wikipedia.org/wiki/Find_(Unix))

###### Find duplicate files (based on MD5 hash)

```bash
find -type f -exec md5sum '{}' ';' | sort | uniq --all-repeated=separate -w 33
```
"""


def fetcher(calls, text=BOOK, ok=True):
    def f(url):
        calls.append(url)
        return Page(url, 200, "text/plain", text, "README.md", "abc") if ok else Page(url, 0, "", "", "", "", "unreachable")
    return f


def test_the_book_is_read_into_tools_and_one_liners_without_the_risky_parts():
    entries = parse(BOOK, CATALOGS[0]["skip_chapters"])
    names = [e["name"] for e in entries]
    assert {"ncdu", "fclones", "nmap", "tshark", "Awesome Home Assistant"} <= set(names)
    assert "Metasploit" not in names and "WAF bypass list" not in names        # attacking: left out
    ncdu = entries[names.index("ncdu")]
    assert ncdu == {"kind": "tool", "name": "ncdu", "url": "https://dev.yorhel.nl/ncdu",
                    "about": "is an easy to use, fast disk usage analyzer.", "chapter": "CLI Tools",
                    "section": "Files and directories"}
    one = entries[names.index("Find duplicate files (based on MD5 hash)")]
    assert one["kind"] == "one-liner" and "md5sum" in one["code"] and one["section"] == "find"


def test_downloaded_once_kept_refreshed_weekly_and_used_offline(tmp_path):
    now = [1_000_000.0]
    calls = []
    cat = Catalogs(tmp_path / "library", fetcher=fetcher(calls), clock=lambda: now[0])
    hits, notes = cat.search("find duplicate files")
    assert hits[0]["name"] in ("fclones", "Find duplicate files (based on MD5 hash)") and not notes
    assert len(calls) == 1 and (tmp_path / "library" / "secret-knowledge.json").exists()
    cat2 = Catalogs(tmp_path / "library", fetcher=fetcher(calls), clock=lambda: now[0] + 3600)
    cat2.search("disk usage")
    assert len(calls) == 1                                                     # the local copy is used
    now[0] += REFRESH_SECONDS + 1
    offline = Catalogs(tmp_path / "library", fetcher=fetcher(calls, ok=False), clock=lambda: now[0])
    hits, notes = offline.search("disk usage")
    assert hits[0]["name"] == "ncdu" and "using the copy from" in notes[0]     # offline: last copy
    empty = Catalogs(tmp_path / "nothing", fetcher=fetcher(calls, ok=False))
    assert empty.search("disk usage") == ([], ["couldn't download The Book of Secret Knowledge (unreachable)"])


def test_search_puts_the_best_match_first(tmp_path):
    cat = Catalogs(tmp_path, fetcher=fetcher([]))
    assert cat.search("nmap")[0][0]["name"] == "nmap"
    assert cat.search("watch network traffic")[0][0]["name"] == "tshark"
    assert cat.search("disk usage", kind="one-liner")[0] == []
    assert cat.search("the")[0] == []                                          # nothing to look for


def test_a_job_can_look_up_a_tool_it_needs(tmp_path):
    model = ScriptedModel([call("catalog__search", query="find duplicate files on my Mac"),
                           say("fclones finds duplicates; its own docs say …")])
    agent = Agent(agent_config(tmp_path), model, tools="local")
    calls = []
    agent.catalogs.fetch = fetcher(calls)
    d = agent.plan("what's a good way to find duplicate files?")
    task = agent.run("what's a good way to find duplicate files?", dispatch=d)
    assert task.status == "done" and "catalog__search" in model.seen_tools[0]
    result = next(m["content"] for m in model.seen[1] if m.get("role") == "tool")
    assert "fclones" in result and "read its own docs" in result
    assert "catalog__search" in agent.lock.graph.states["knowledge"]["tools"]
    assert agent.paths.library in agent.deny_w                                 # no tool can poison the copy
    assert json.loads((agent.paths.library / "secret-knowledge.json").read_text())["license"] == "MIT"
    agent.close()
