"""
Cortes de GTA ao vivo e de graça.
Enquanto o streamer está ao vivo na Twitch, pega os clipes que os espectadores criam,
edita em vertical com a sua foto, escreve o título (Gemini grátis) e posta como Short no YouTube.
Roda sozinho no GitHub Actions (veja .github/workflows/cortes.yml).

  python main.py checar   -> só diz se tem live agora
  python main.py live     -> fica de olho na live e vai postando (padrão)
  python main.py teste    -> posta 1 clipe como PRIVADO para testar as chaves
"""
import base64
import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------- AJUSTES ----------
JOGO = "Grand Theft Auto V"
SO_GTA = True        # ignora clipes de outros jogos
VIEWS_MIN = 10       # só posta clipe com pelo menos X views
DURACAO_MIN = 10     # segundos
MAX_POR_HORA = 3     # limite de Shorts por hora
JANELA_MIN = 25      # quanto tempo cada execução fica de olho na live
SONDAGEM = 60        # segundos entre uma olhada e outra na Twitch
MODELOS = ["gemini-3.8-flash", "gemini-3.6-flash"]  # Gemini grátis, por ordem de preferência
FONTE = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
POSTADOS = Path("posted.json")
TMP = Path("tmp")
STREAMERS = [s.strip().lower() for s in os.environ.get("STREAMERS", "").split(",") if s.strip()]


# ---------- tempo e estado ----------
def agora():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ler_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def carregar_estado():
    """{id_do_clipe: [quando_postou, quando_foi_criado, id_do_canal]}"""
    if not POSTADOS.exists():
        return {}
    dados = json.loads(POSTADOS.read_text())
    if isinstance(dados, list):  # formato antigo
        return {i: ["", "", ""] for i in dados}
    return dados


def salvar_estado(estado):
    POSTADOS.write_text(json.dumps(estado, indent=1))
    if os.environ.get("GITHUB_ACTIONS"):
        subprocess.run(
            "git config user.name cortes-bot && git config user.email cortes-bot@users.noreply.github.com"
            " && git add posted.json && (git diff --cached --quiet"
            " || (git commit -qm 'clipes postados' && git pull -q --rebase && git push -q))",
            shell=True)


# ---------- Twitch ----------
class Twitch:
    def __init__(self):
        self.cid = os.environ["TWITCH_CLIENT_ID"]
        self.segredo = os.environ["TWITCH_CLIENT_SECRET"]
        self.token = None

    def get(self, caminho, **params):
        for _ in range(2):
            if not self.token:
                r = requests.post("https://id.twitch.tv/oauth2/token", timeout=30, data={
                    "client_id": self.cid, "client_secret": self.segredo,
                    "grant_type": "client_credentials"})
                r.raise_for_status()
                self.token = r.json()["access_token"]
            r = requests.get("https://api.twitch.tv/helix/" + caminho, params=params, timeout=30,
                             headers={"Client-Id": self.cid, "Authorization": f"Bearer {self.token}"})
            if r.status_code == 401:  # token vencido: pega outro e tenta de novo
                self.token = None
                continue
            r.raise_for_status()
            return r.json()["data"]
        raise RuntimeError("A Twitch recusou o token")


def ao_vivo(tw):
    """Canais da lista que estão ao vivo (a Twitch só devolve quem está ao vivo)."""
    vivos = tw.get("streams", user_login=STREAMERS)
    return [v for v in vivos if v["type"] == "live" and (not SO_GTA or v["game_name"] == JOGO)]


def repetido(c, estado):
    """Espectadores costumam clipar o mesmo momento várias vezes: posta só um."""
    criado = ler_iso(c["created_at"])
    for postado, criado_outro, canal in estado.values():
        if postado and canal == c["broadcaster_id"] and \
                abs((criado - ler_iso(criado_outro)).total_seconds()) < 120:
            return True
    return False


def escolher_clipe(tw, vivos, estado):
    inicio = iso(agora() - timedelta(hours=2))
    candidatos = []
    for v in vivos:
        for c in tw.get("clips", broadcaster_id=v["user_id"], started_at=inicio, first=30):
            if c["id"] in estado or c["duration"] < DURACAO_MIN or c["view_count"] < VIEWS_MIN:
                continue
            if SO_GTA and c["game_id"] != v["game_id"]:
                continue
            if repetido(c, estado):
                continue
            candidatos.append(c)
    return max(candidatos, key=lambda c: c["view_count"], default=None)


def postados_na_ultima_hora(estado):
    corte = agora() - timedelta(hours=1)
    return sum(1 for postado, _, _ in estado.values() if postado and ler_iso(postado) > corte)


# ---------- vídeo ----------
def baixar(url, saida):
    subprocess.run(["yt-dlp", "-q", "--no-playlist", "--force-overwrites",
                    "-f", "best[height<=1080]/best", "-o", str(saida), url], check=True)


def extrair_frame(video, saida, segundo):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(segundo), "-i", str(video),
                    "-frames:v", "1", "-vf", "scale=768:-2", str(saida)], check=True)


def titulo_ia(frame, titulo_original, streamer):
    """Gemini (grátis) olha uma cena e escreve o título. Sem chave ou com erro: usa o título do clipe."""
    reserva = f"{streamer}: {titulo_original}"[:60]
    chave = os.environ.get("GEMINI_API_KEY")
    if not chave:
        return reserva
    imagem = base64.b64encode(Path(frame).read_bytes()).decode()
    pedido = {"contents": [{"parts": [
        {"inline_data": {"mime_type": "image/jpeg", "data": imagem}},
        {"text": (
            f"Esta é uma cena de um clipe de GTA do streamer {streamer}. "
            f"O título original do clipe é: {titulo_original!r}. "
            "Escreva UM título em português do Brasil, com no máximo 60 caracteres, "
            "dizendo o que o streamer está fazendo nesse momento. "
            "Sem emojis, sem aspas, sem hashtags. Responda só com o título.")},
    ]}]}
    for modelo in MODELOS:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent",
                headers={"x-goog-api-key": chave}, json=pedido, timeout=60)
            r.raise_for_status()
            partes = r.json()["candidates"][0]["content"]["parts"]
            texto = "".join(p.get("text", "") for p in partes).strip()
            titulo = texto.splitlines()[0].strip().strip('"“”*')[:60]
            if titulo:
                return titulo
        except Exception as e:
            print("Gemini falhou em", modelo, "->", e)
    return reserva


def cartao_texto(titulo, streamer, saida):
    """PNG transparente com o título e o crédito do streamer."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (1080, 420), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    grande = ImageFont.truetype(FONTE, 64)
    pequena = ImageFont.truetype(FONTE, 38)
    linhas = "\n".join(textwrap.wrap(titulo.upper(), 22))
    d.multiline_text((540, 0), linhas, font=grande, fill="white", anchor="ma", align="center",
                     spacing=14, stroke_width=6, stroke_fill="black")
    d.text((540, 360), f"Clipe: {streamer}", font=pequena, fill="white", anchor="ma",
           stroke_width=4, stroke_fill="black")
    img.save(saida)


def editar(clip, foto, cartao, saida):
    """Vertical 1080x1920: fundo desfocado, clipe no meio, sua foto no topo, título embaixo."""
    filtro = (
        "[0:v]split=2[a][b];"
        "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "scale=108:192,boxblur=3:2,scale=1080:1920[bg];"
        "[b]scale=1080:-2[fg];"
        "[bg][fg]overlay=0:(H-h)/2[v1];"
        "[1:v]scale=w=1000:h=380:force_original_aspect_ratio=decrease[foto];"
        "[v1][foto]overlay=(W-w)/2:150[v2];"
        "[v2][2:v]overlay=0:1330[v]"
    )
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(clip), "-i", str(foto), "-i", str(cartao),
                    "-filter_complex", filtro, "-map", "[v]", "-map", "0:a?",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(saida)], check=True)


# ---------- YouTube ----------
def enviar(video, titulo, clipe, privacidade="public"):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    cred = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    yt = build("youtube", "v3", credentials=cred)
    corpo = {
        "snippet": {
            "title": f"{titulo} #Shorts"[:100],
            "description": (f"Clipe de {clipe['broadcaster_name']} na Twitch: {clipe['url']}\n\n"
                            "#Shorts #GTA #GTARP"),
            "categoryId": "20",
            "tags": ["gta", "gta rp", "clipes", "shorts"],
        },
        "status": {"privacyStatus": privacidade, "selfDeclaredMadeForKids": False},
        "paidProductPlacementDetails": {"hasPaidProductPlacement": False},
    }
    req = yt.videos().insert(
        part="snippet,status,paidProductPlacementDetails",
        body=corpo,
        media_body=MediaFileUpload(str(video), mimetype="video/mp4", resumable=True),
    )
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp["id"]


# ---------- fluxo ----------
def achar_foto():
    foto = next(Path(".").glob("foto.*"), None)
    if not foto:
        raise SystemExit("Falta a sua foto no repositório, com o nome foto.png")
    return foto


def publicar(c, foto, estado, teste=False):
    bruto, frame, card, final = (TMP / n for n in ("bruto.mp4", "frame.jpg", "card.png", "final.mp4"))
    TMP.mkdir(exist_ok=True)
    print("Clipe:", c["id"], "|", c["broadcaster_name"], "|", c["title"], "|", c["view_count"], "views")
    try:
        baixar(c["url"], bruto)
        extrair_frame(bruto, frame, c["duration"] / 2)
        titulo = titulo_ia(frame, c["title"], c["broadcaster_name"])
        cartao_texto(titulo, c["broadcaster_name"], card)
        editar(bruto, foto, card, final)
    except Exception as e:  # clipe com problema: marca e segue para o próximo
        print("Falhou ao preparar, pulando:", e)
        if teste:
            raise
        estado[c["id"]] = ["", c["created_at"], c["broadcaster_id"]]
        salvar_estado(estado)
        return
    print("Título:", titulo)
    video_id = enviar(final, ("[TESTE] " if teste else "") + titulo, c, "private" if teste else "public")
    print("Enviado: https://youtube.com/shorts/" + video_id)
    if not teste:
        estado[c["id"]] = [iso(agora()), c["created_at"], c["broadcaster_id"]]
        salvar_estado(estado)


def modo_live():
    if not STREAMERS:
        raise SystemExit("Crie a variável STREAMERS com o(s) canal(is) da Twitch, ex.: canal1,canal2")
    foto = achar_foto()
    tw = Twitch()
    estado = carregar_estado()
    fim = time.time() + JANELA_MIN * 60
    sem_live = 0
    while time.time() < fim:
        try:
            vivos = ao_vivo(tw)
            if not vivos:
                sem_live += 1
                if sem_live >= 3:
                    print("A live acabou.")
                    break
            else:
                sem_live = 0
                if postados_na_ultima_hora(estado) < MAX_POR_HORA:
                    c = escolher_clipe(tw, vivos, estado)
                    if c:
                        publicar(c, foto, estado)
        except requests.RequestException as e:  # Twitch ou Gemini com soluço: tenta de novo no próximo ciclo
            print("Erro de rede, tentando de novo:", e)
        time.sleep(SONDAGEM)


def checar():
    if not STREAMERS:
        print("Falta a variável STREAMERS (Settings > Secrets and variables > Actions > Variables).")
        vivos = []
    else:
        vivos = ao_vivo(Twitch())
    saida = os.environ.get("GITHUB_OUTPUT")
    if saida:
        with open(saida, "a") as f:
            f.write(f"aovivo={'true' if vivos else 'false'}\n")
    print("Ao vivo agora:", [v["user_login"] for v in vivos] or "ninguém")


def teste():
    if not STREAMERS:
        raise SystemExit("Crie a variável STREAMERS com o(s) canal(is) da Twitch, ex.: canal1,canal2")
    tw = Twitch()
    foto = achar_foto()
    inicio = iso(agora() - timedelta(days=7))
    candidatos = []
    for u in tw.get("users", login=STREAMERS):
        candidatos += tw.get("clips", broadcaster_id=u["id"], started_at=inicio, first=20)
    candidatos = [c for c in candidatos if c["duration"] >= DURACAO_MIN]
    if not candidatos:
        raise SystemExit("Nenhum clipe nos últimos 7 dias desses canais.")
    publicar(max(candidatos, key=lambda c: c["view_count"]), foto, {}, teste=True)


if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else "live"
    {"checar": checar, "teste": teste, "live": modo_live}[modo]()
