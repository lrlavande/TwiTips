import os
import re
import json
import requests
import subprocess
import anthropic
from pathlib import Path

# ── Config ──────────────────────────────────────────────
TWITCH_CLIENT_ID     = os.environ['TWITCH_CLIENT_ID']
TWITCH_CLIENT_SECRET = os.environ['TWITCH_CLIENT_SECRET']
ANTHROPIC_API_KEY    = os.environ['ANTHROPIC_API_KEY']
STREAMER_LOGIN       = 'axiominus'
PROCESSED_FILE = 'scripts/processed_vods.txt'
INDEX_FILE           = 'index.html'

# ── Twitch API ───────────────────────────────────────────
def get_twitch_token():
    r = requests.post('https://id.twitch.tv/oauth2/token', data={
        'client_id': TWITCH_CLIENT_ID,
        'client_secret': TWITCH_CLIENT_SECRET,
        'grant_type': 'client_credentials'
    })
    return r.json()['access_token']

def get_user_id(token, login):
    r = requests.get('https://api.twitch.tv/helix/users', 
        params={'login': login},
        headers={'Client-ID': TWITCH_CLIENT_ID, 'Authorization': f'Bearer {token}'}
    )
    return r.json()['data'][0]['id']

def get_recent_vods(token, user_id):
    r = requests.get('https://api.twitch.tv/helix/videos',
        params={'user_id': user_id, 'type': 'archive', 'first': 5},
        headers={'Client-ID': TWITCH_CLIENT_ID, 'Authorization': f'Bearer {token}'}
    )
    return r.json().get('data', [])

# ── Processed VODs ───────────────────────────────────────
def load_processed():
    if not Path(PROCESSED_FILE).exists():
        return set()
    return set(Path(PROCESSED_FILE).read_text().strip().split('\n'))

def save_processed(vod_ids):
    Path(PROCESSED_FILE).write_text('\n'.join(sorted(vod_ids)))

# ── Download & transcribe ────────────────────────────────
def download_audio(vod_id):
    out = f'/tmp/vod_{vod_id}.mp3'
    if Path(out).exists():
        return out
    subprocess.run([
        'python', '-m', 'yt_dlp',
        '-x', '--audio-format', 'mp3',
        '-o', out,
        f'https://www.twitch.tv/videos/{vod_id}'
    ], check=True)
    return out

def transcribe(audio_path):
    from faster_whisper import WhisperModel
    model = WhisperModel('small', device='cpu', compute_type='int8')
    segments, _ = model.transcribe(
        audio_path, language='fr',
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500)
    )
    lines = []
    for seg in segments:
        lines.append(f"[{seg.start:.1f}s] {seg.text.strip()}")
    return '\n'.join(lines)

# ── Extract tips with Claude ─────────────────────────────
def extract_tips(transcript, vod_id, streamer):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""Voici la transcription d'un live Twitch d'un concept artist ({streamer}).
Extrait UNIQUEMENT les conseils de dessin / design art concrets et actionnables.

Pour chaque conseil trouvé, retourne un objet JSON avec :
- "text": le conseil reformulé clairement (1-3 phrases max)
- "cat": la catégorie parmi ["meca", "render", "design", "silhouette", "workflow", "portfolio", "mindset", "pratique"]
- "tc_seconds": le timestamp en secondes dans le VOD où il le dit

Retourne UNIQUEMENT un tableau JSON valide, rien d'autre.
Si tu ne trouves aucun conseil, retourne [].

Transcription :
{transcript[:15000]}"""

    message = client.messages.create(
        model='claude-opus-4-6',
        max_tokens=4000,
        messages=[{'role': 'user', 'content': prompt}]
    )

    raw = message.content[0].text.strip()
    raw = re.sub(r'^```json\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    return json.loads(raw)

# ── Convert seconds to timecode ──────────────────────────
def seconds_to_tc(s):
    s = int(s)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h > 0:
        return f"{h}h{m:02d}m{sec:02d}s"
    return f"{m}m{sec:02d}s"

# ── Inject new tips into index.html ─────────────────────
def inject_tips(new_tips, vod_id, streamer):
    html = Path(INDEX_FILE).read_text(encoding='utf-8')

    # Trouver le dernier id existant
    ids = re.findall(r'\{ id:(\d+),', html)
    next_id = max(int(i) for i in ids) + 1 if ids else 61

    new_js = ''
    for tip in new_tips:
        tc = seconds_to_tc(tip.get('tc_seconds', 0))
        text = tip['text'].replace("'", "\\'").replace('"', '\\"')
        cat = tip.get('cat', 'design')
        new_js += f"\n  {{ id:{next_id}, cat:'{cat}', streamer:'{streamer}', text:\"{text}\", vod:'{vod_id}', tc:'{tc}' }},"
        next_id += 1

    # Insérer avant la fermeture du tableau TIPS
    html = html.replace(
        '];\n\nlet deleted',
        f'{new_js}\n];\n\nlet deleted'
    )

    # Mettre à jour le compteur de stats
    total = len(re.findall(r'\{ id:\d+,', html))
    html = re.sub(r'<strong id="stat-total">\d+</strong>', f'<strong id="stat-total">{total}</strong>', html)

    Path(INDEX_FILE).write_text(html, encoding='utf-8')
    print(f"[OK] {len(new_tips)} nouveaux tips injectés dans index.html")

# ── Main ─────────────────────────────────────────────────
def main():
    print("🔍 Vérification des nouveaux VODs...")

    token      = get_twitch_token()
    user_id    = get_user_id(token, STREAMER_LOGIN)
    vods       = get_recent_vods(token, user_id)
    processed  = load_processed()

    new_vods = [v for v in vods if v['id'] not in processed]

    if not new_vods:
        print("✅ Aucun nouveau VOD, rien à faire.")
        return

    for vod in new_vods:
        vod_id = vod['id']
        print(f"\n📺 Nouveau VOD détecté : {vod_id} — {vod['title']}")

        try:
            print("⬇ Téléchargement audio...")
            audio = download_audio(vod_id)

            print("🎙 Transcription...")
            transcript = transcribe(audio)

            print("🤖 Extraction des tips avec Claude...")
            tips = extract_tips(transcript, vod_id, 'Axiominus')

            if tips:
                print(f"✨ {len(tips)} tips trouvés !")
                inject_tips(tips, vod_id, 'Axiominus')
            else:
                print("😶 Aucun conseil trouvé dans ce VOD.")

            processed.add(vod_id)
            save_processed(processed)

        except Exception as e:
            print(f"❌ Erreur sur VOD {vod_id} : {e}")
            continue

    print("\n✅ Mise à jour terminée !")

if __name__ == '__main__':
    main()
