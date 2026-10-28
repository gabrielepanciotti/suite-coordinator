"""#83 F0: una chat appartiene a una sola istanza; un profilo raggiunge solo le sue."""

import json

import instance_acl
import profiles
from profiles import Profile

ADMIN = Profile("owner", "Owner", is_admin=True, projects=())
MEMBER = next(p for p in profiles.PROFILES if p.key == "member")


def test_una_chat_dichiarata_due_volte_e_contesa():
    idx = instance_acl.build_chat_index([("acme", -1), ("personal", -1), ("energypass", -2)])
    assert idx.is_contested(-1) and idx.contested[-1] == ("acme", "personal")
    assert idx.instance_of(-1) is None
    assert idx.instance_of(-2) == "energypass"


def test_le_istanze_senza_gruppo_non_contano():
    idx = instance_acl.build_chat_index([("a", None), ("b", None), ("c", -5)])
    assert idx.contested == {} and idx.owner == {-5: "c"}


def test_conflitto_per_la_scrittura():
    idx = instance_acl.build_chat_index([("acme", -1)])
    assert instance_acl.conflict_for(-1, "personal", idx) == "acme"
    assert instance_acl.conflict_for(-1, "acme", idx) is None
    assert instance_acl.conflict_for(-9, "personal", idx) is None


def test_default_deny():
    assert not instance_acl.authorize(None, "acme")
    assert not instance_acl.authorize(None, None)


def test_admin_raggiunge_tutte_le_istanze():
    assert instance_acl.authorize(ADMIN, "acme")
    assert instance_acl.authorize(ADMIN, "una-che-nascera")


def test_member_e_un_partecipante_di_personal():
    assert instance_acl.authorize(MEMBER, "personal")
    assert not instance_acl.authorize(MEMBER, "acme")
    assert not instance_acl.authorize(MEMBER, "energypass")


def test_chat_senza_istanza_resta_ai_controlli_di_progetto():
    assert instance_acl.authorize(MEMBER, None)


def test_l_env_restringe_ma_non_allarga(monkeypatch):
    monkeypatch.setenv("PROFILE_MEMBER_INSTANCES", "personal,acme")
    assert profiles.instances_of(MEMBER) == ("personal",)
    assert not instance_acl.authorize(MEMBER, "acme")
    monkeypatch.setenv("PROFILE_MEMBER_INSTANCES", "")
    assert not instance_acl.authorize(MEMBER, "personal")


def test_indice_dal_registro(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    reg = {"version": 1, "instances": {}}
    for band, (iid, chat) in enumerate([("acme", -100), ("personal", -100), ("energypass", None)], 1):
        path = tmp_path / iid
        path.mkdir()
        tg = {"chatId": chat, "agent": "g"} if chat else {}
        (path / "instance.config.json").write_text(json.dumps(
            {"profile": iid, "channels": {"telegram": tg, "bus": {"tenant": f"life-{iid}"}}}))
        reg["instances"][iid] = {"path": str(path), "profile": iid, "band": band}
    (home / "instances.json").write_text(json.dumps(reg))
    idx = instance_acl.index_from_registry({"LIFE_HOME": str(home)})
    assert idx.contested == {-100: ("acme", "personal")}
    assert instance_acl.describe_contested(idx)[0].startswith("chat -100 dichiarata da acme e personal")


def test_registro_illeggibile_da_indice_vuoto(tmp_path):
    (tmp_path / "instances.json").write_text("{rotto")
    idx = instance_acl.index_from_registry({"LIFE_HOME": str(tmp_path)})
    assert idx.owner == {} and idx.contested == {}
