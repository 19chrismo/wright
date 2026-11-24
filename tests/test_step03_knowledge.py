"""Step 3: knowledge-pack pipeline. Grounding is recorded before acting, and cannot be faked."""
from wright.demos import step3
from wright.knowledge import (Decision, KnowledgePack, SemanticStore, capture_url, record_version)
from wright.testkit import VENDOR_DOC, StaticSite


def test_grounding_before_acting_and_fakes_rejected():
    r = step3(verbose=False)
    t, pack = r["task"], r["pack"]
    assert t.status == "done"
    blocked = r["events"].of("blocked")
    assert blocked and blocked[0]["tool"] == "shell__run"          # mutation before grounding: blocked
    errors = [e["text"] for e in r["events"].of("tool_result") if e["is_error"]]
    assert any("excerpt not found" in e for e in errors)            # invented quote rejected
    assert any("[SOURCED] requires a captured" in e for e in errors)  # tag without source rejected
    assert [d.tag for d in pack.decisions] == ["SOURCED", "INFERRED"]
    assert pack.system_versions["openssh"]["version"] == "OpenSSH_9.6p1"


def _site_pack():
    site = StaticSite({"/d": ("text/html", VENDOR_DOC)})
    return site, KnowledgePack(id="kp", domain="ssh", task_type="harden")


def test_version_mismatch_blocks_sourced():
    site, pack = _site_pack()
    record_version(pack, "openssh", "echo OpenSSH_7.4p1")
    src, _ = capture_url(pack, site.url + "/d", 1, "The default is yes.", "9.6", "openssh")
    err = pack.add_decision(Decision("D1", "x", "SOURCED", 0.9, [src.id]))
    assert err and "version mismatch" in err
    site.stop()


def test_unreachable_source_cannot_ground():
    pack = KnowledgePack(id="kp", domain="ssh", task_type="harden")
    src, msg = capture_url(pack, "http://127.0.0.1:9/nothing", 1, "anything")
    assert src and not src.reachable and "UNREACHABLE" in msg
    assert "requires a captured, reachable" in pack.add_decision(Decision("D1", "x", "SOURCED", 0.9, [src.id]))


def test_unknown_needs_mitigation_and_becomes_sandbox_first():
    pack = KnowledgePack(id="kp", domain="x", task_type="y")
    assert "mitigation" in pack.add_decision(Decision("D1", "guess", "UNKNOWN", 0.3))
    d = Decision("D2", "guess", "UNKNOWN", 0.3, mitigation="sandbox_test")
    assert pack.add_decision(d) is None and d.mode == "sandbox-test-first"


def test_community_sources_need_two_independent_hosts():
    pack = KnowledgePack(id="kp", domain="x", task_type="y")
    a = StaticSite({"/a": ("text/plain", "use flag --fast for speed")})
    s1, _ = capture_url(pack, a.url + "/a", 5, "use flag --fast")
    assert "two independent hosts" in pack.add_decision(Decision("D1", "x", "SOURCED", 0.8, [s1.id]))
    mirror, _ = capture_url(pack, a.url.replace("127.0.0.1", "localhost") + "/a", 5, "use flag --fast")
    assert "independent" in pack.add_decision(Decision("D2", "x", "SOURCED", 0.8, [s1.id, mirror.id]))
    b = StaticSite({"/b": ("text/plain", "I always use flag --fast; it halves build time")})
    s2, _ = capture_url(pack, b.url.replace("127.0.0.1", "localhost") + "/b", 5, "use flag --fast")
    assert pack.add_decision(Decision("D3", "x", "SOURCED", 0.8, [s1.id, s2.id])) is None
    a.stop(); b.stop()


def test_semantic_store_versions_and_retrieves_by_domain(tmp_path):
    store = SemanticStore(tmp_path)
    pack = KnowledgePack(id="kp1", domain="router", task_type="harden", intent="harden wan ssh")
    store.save(pack)
    pack.decisions.append(Decision("D1", "disable wan ssh", "INFERRED", 0.6, chain="c"))
    store.save(pack)
    assert store.save(pack).name == "kp1.v2.json"                 # unchanged → no new version
    assert store.load("router", "kp1", 1).decisions == []
    assert store.load("router", "kp1").version == 2
    assert store.retrieve("router", "wan ssh")[0].id == "kp1"
    assert store.retrieve("home-assistant") == []
