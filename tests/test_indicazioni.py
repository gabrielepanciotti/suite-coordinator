"""Il diario e la pubblicazione: le due garanzie che una frase non si perda."""
from __future__ import annotations

import subprocess

import indicazioni


def _repo(tmp_path):
    """Un repo vero e minuscolo: git qui non si finge, si esegue."""
    r = tmp_path / "nodo"
    r.mkdir()
    subprocess.run(("git", "init", "-q", "-b", "master", str(r)), check=True)
    subprocess.run(("git", "-C", str(r), "config", "user.email", "t@t"), check=True)
    subprocess.run(("git", "-C", str(r), "config", "user.name", "T"), check=True)
    (r / "primo.txt").write_text("x", encoding="utf-8")
    subprocess.run(("git", "-C", str(r), "add", "-A"), check=True)
    subprocess.run(("git", "-C", str(r), "commit", "-qm", "init"), check=True)
    return r


def test_la_prima_indicazione_crea_il_diario(tmp_path):
    p = indicazioni.registra(tmp_path, autore="Gab", testo="togli i gatti")
    assert p is not None and p.exists()
    testo = p.read_text(encoding="utf-8")
    assert "Indicazioni ricevute in chat" in testo
    assert "togli i gatti" in testo
    assert "Gab" in testo


def test_le_indicazioni_si_accumulano_e_non_si_sovrascrivono(tmp_path):
    """Append-only: e' l'unica proprieta' per cui il diario esiste."""
    indicazioni.registra(tmp_path, autore="Gab", testo="prima")
    p = indicazioni.registra(tmp_path, autore="Gab", testo="seconda")
    testo = p.read_text(encoding="utf-8")
    assert "prima" in testo and "seconda" in testo
    assert testo.count("Indicazioni ricevute in chat") == 1


def test_un_messaggio_vuoto_non_sporca_il_diario(tmp_path):
    assert indicazioni.registra(tmp_path, autore="Gab", testo="   ") is None
    assert not (tmp_path / indicazioni.DIARIO).exists()


def test_un_messaggio_enorme_viene_troncato(tmp_path):
    p = indicazioni.registra(tmp_path, autore="Gab",
                             testo="a" * (indicazioni.MAX_CARATTERI + 500))
    assert "[…troncato]" in p.read_text(encoding="utf-8")


def test_un_workspace_impossibile_non_fa_saltare_il_turno(tmp_path):
    """Il diario e' una rete di sicurezza: se cade lei si risponde lo stesso."""
    ostacolo = tmp_path / "file"
    ostacolo.write_text("non sono una cartella", encoding="utf-8")
    assert indicazioni.registra(ostacolo, autore="Gab", testo="ciao") is None


def test_il_lavoro_sporco_viene_committato(tmp_path):
    r = _repo(tmp_path)
    (r / "nuovo.txt").write_text("lavoro del turno", encoding="utf-8")
    indicazioni.pubblica(r, motivo="prova")
    assert not subprocess.run(
        ("git", "-C", str(r), "status", "--porcelain"),
        capture_output=True, text=True).stdout.strip()
    log = subprocess.run(("git", "-C", str(r), "log", "-1", "--pretty=%s"),
                         capture_output=True, text=True).stdout
    assert "prova" in log


def test_i_segreti_non_entrano_nel_commit_automatico(tmp_path):
    """29/09/2026: tre backup di .env.production finiti su GitHub da qui."""
    r = _repo(tmp_path)
    (r / "backend").mkdir()
    (r / "backend" / ".env.production.bak-20260831").write_text("X=1", encoding="utf-8")
    (r / "backend" / ".env.example").write_text("X=", encoding="utf-8")
    (r / "codice.py").write_text("print(1)", encoding="utf-8")
    indicazioni.pubblica(r, motivo="prova")
    messi = subprocess.run(
        ("git", "-C", str(r), "show", "--name-only", "--pretty=", "HEAD"),
        capture_output=True, text=True).stdout.split()
    assert "codice.py" in messi
    assert "backend/.env.example" in messi
    assert "backend/.env.production.bak-20260831" not in messi


def test_i_file_nuovi_grandi_restano_fuori_dal_commit(tmp_path, monkeypatch):
    """#96: 9 PDF scaricati nel gruppo finirono in git (~740 MB)."""
    monkeypatch.setattr(indicazioni, "MAX_FILE_MB", 0.001)
    r = _repo(tmp_path)
    (r / "libro.pdf").write_bytes(b"0" * 5000)
    (r / "piccolo.txt").write_text("x", encoding="utf-8")
    indicazioni.pubblica(r, motivo="prova")
    messi = subprocess.run(
        ("git", "-C", str(r), "show", "--name-only", "--pretty=", "HEAD"),
        capture_output=True, text=True).stdout.split()
    assert "piccolo.txt" in messi
    assert "libro.pdf" not in messi


def test_solo_segreti_nessun_commit(tmp_path):
    r = _repo(tmp_path)
    prima = subprocess.run(("git", "-C", str(r), "rev-parse", "HEAD"),
                           capture_output=True, text=True).stdout
    (r / ".env").write_text("X=1", encoding="utf-8")
    assert indicazioni.pubblica(r, motivo="prova") is False
    dopo = subprocess.run(("git", "-C", str(r), "rev-parse", "HEAD"),
                          capture_output=True, text=True).stdout
    assert prima == dopo


def test_sembra_un_segreto():
    for s in (".env", "backend/.env.production", "a/.env.production.bak.17880",
              "x/.credentials.json", "k/release.keystore", "c/server.pem"):
        assert indicazioni.sembra_un_segreto(s), s
    for s in (".env.example", "backend/.env.production.example", "app.py",
              "docs/env.md", ".envrc.md"):
        assert not indicazioni.sembra_un_segreto(s), s


def test_un_turno_di_sola_lettura_non_crea_commit(tmp_path):
    """Leggere il DB non e' un cambiamento: un commit vuoto per ogni domanda
    riempirebbe la storia di rumore."""
    r = _repo(tmp_path)
    prima = subprocess.run(("git", "-C", str(r), "rev-parse", "HEAD"),
                           capture_output=True, text=True).stdout
    assert indicazioni.pubblica(r, motivo="niente") is False
    dopo = subprocess.run(("git", "-C", str(r), "rev-parse", "HEAD"),
                          capture_output=True, text=True).stdout
    assert prima == dopo


def test_senza_remoto_il_commit_resta_comunque(tmp_path):
    """Il push fallisce (nessun origin) ma il lavoro e' gia' salvo: al turno
    dopo riparte da li', invece di essere perso."""
    r = _repo(tmp_path)
    (r / "nuovo.txt").write_text("x", encoding="utf-8")
    assert indicazioni.pubblica(r, motivo="senza remoto") is False
    assert "senza remoto" in subprocess.run(
        ("git", "-C", str(r), "log", "-1", "--pretty=%s"),
        capture_output=True, text=True).stdout


def test_una_cartella_che_non_e_un_repo_non_esplode(tmp_path):
    assert indicazioni.pubblica(tmp_path, motivo="niente repo") is False
