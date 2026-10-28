# tests/test_photo_ocr.py
"""Un backend/modello senza vision (vedi AI_BACKEND in claude_runner.py) non
vede l'immagine dietro il path che ``_photo_prompt`` gli passa: prima
rispondeva "non supporto le immagini". ``_ocr_image`` estrae il testo per
default, cosi' ogni backend riceve qualcosa di leggibile senza doverci
riuscire da solo con un giro di OCR improvvisato."""
import bot


def test_ocr_image_never_raises_on_missing_file():
    assert bot._ocr_image("/percorso/che/non/esiste.jpg") == ""


def test_photo_prompt_single_with_ocr_text():
    prompt = bot._photo_prompt(["/a/foto.jpg"], "", ["CODICE 12345"])
    assert prompt.startswith("[CMD:/dispensa] /a/foto.jpg")
    assert "Testo estratto dalla foto (OCR):\nCODICE 12345" in prompt


def test_photo_prompt_single_without_ocr_text_unchanged():
    prompt = bot._photo_prompt(["/a/foto.jpg"], "una nota", [""])
    assert prompt == "[CMD:/dispensa] /a/foto.jpg una nota"
    assert "OCR" not in prompt


def test_photo_prompt_album_with_partial_ocr():
    prompt = bot._photo_prompt(
        ["/a/1.jpg", "/a/2.jpg"], "", ["testo dalla prima", ""]
    )
    assert "Testo estratto (OCR) per foto:" in prompt
    assert "/a/1.jpg:\ntesto dalla prima" in prompt
    assert "/a/2.jpg" not in prompt.split("Testo estratto (OCR) per foto:")[1]


def test_photo_prompt_album_no_ocr_unchanged():
    prompt = bot._photo_prompt(["/a/1.jpg", "/a/2.jpg"], "", ["", ""])
    assert "OCR" not in prompt
