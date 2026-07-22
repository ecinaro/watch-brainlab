---
name: watch-brainlab
version: "1.0.0"
description: Watch a video (URL or local path). Downloads with yt-dlp, extracts auto-scaled frames with ffmpeg, pulls the transcript from captions (or Whisper API fallback), and outputs a structured report with frame paths and timestamped transcript for the agent to analyze.
argument-hint: "<video-url-or-path> [question]"
author: ecinaro
license: MIT
user-invocable: true
---

# /watch-brainlab

Video analiz aracı. Bir Python script videoyu indirir, frame'leri çıkarır, altyazıları/transkripti alır ve markdown rapor üretir. Agent bu raporu okuyarak video hakkında sorulara cevap verir.

## SKILL_DIR'ı Belirle (her komuttan önce)

Bu dosyanın bulunduğu dizinin mutlak yolunu `SKILL_DIR` olarak kullan. Script'ler `SKILL_DIR/scripts/` altında:

```
SKILL_DIR="<bu SKILL.md dosyasının bulunduğu dizinin mutlak yolu>"
```

**Python interpreter:** Windows'ta `python`, macOS/Linux'ta `python3` kullan — ya da sisteminizde Python 3'ü çalıştıran komutu.

## Setup (ilk kullanımda)

İlk çalıştırmada:

```bash
python "${SKILL_DIR}/scripts/setup.py" --json
```

Bu komut JSON çıktı verir. `can_proceed` ve `first_run` alanlarına bak:

| can_proceed | first_run | Aksiyon |
|---|---|---|
| `true` | `false` | Hazır. Devam et. |
| `true` | `true` | Çalışır ama Whisper anahtarı önerilir. |
| `false` | `true` | İlk kurulum: `setup.py` çalıştır, eksikleri kur. |
| `false` | `false` | Ortam bozulmuş: `setup.py` çalıştır, eksikleri kur. |

Eksik bağımlılıklar için:
```bash
python "${SKILL_DIR}/scripts/setup.py"
```

macOS'ta brew ile otomatik kurulur. Windows/Linux'ta kurulum komutlarını ekrana basar.

### Whisper API Anahtarı İsteme & Yapılandırma Rehberi (Agent Talimatı)

`has_api_key: false` ise veya ilk çalıştırmada (`first_run: true`), **Agent kullanıcıya Whisper API anahtarını sormalı ve yönlendirmelidir:**

1. **Kullanıcıya Açıkla:**
   * Altyazısı olmayan videoların (Instagram Reels, TikTok, yerel video dosyaları vb.) seslerini metne dönüştürmek için bir Whisper API anahtarı önerilir.
   * **Ücretsiz / Hızlı Seçenek (Önerilen):** [Groq Console](https://console.groq.com/keys) adresinden anında ücretsiz bir `GROQ_API_KEY` alınabilir.
   * **Alternatif:** [OpenAI API Keys](https://platform.openai.com/api-keys) adresinden `OPENAI_API_KEY` kullanılabilir.

2. **Kullanıcı Anahtarı Sağlarsa:**
   Agent anahtarı doğrudan `~/.config/brainwatch/.env` dosyasına yazar:
   ```env
   GROQ_API_KEY=gsk_...
   ```
   veya
   ```env
   OPENAI_API_KEY=sk-...
   ```
   Ardından `SETUP_COMPLETE=true` ekler.

3. **Kullanıcı Anahtar Vermek İstemezse:**
   Anahtar zorunlu değildir. Kullanıcı vermek istemezse veya atlarsa `.env` dosyasına `SETUP_COMPLETE=true` yazılır ve bir daha sorulmaz. Altyazısı olmayan videolar sadece görüntü kareleri (frames-only) ile analiz edilir.

Kullanıcıya bir kere detail tercihi sor:
- `transcript` — sadece transkript, frame yok
- `efficient` — hızlı keyframe (maks 50)
- `balanced` (önerilen) — sahne-algılayan frame (maks 100)
- `token-burner` — sahne-algılayan, limitsiz (yüksek token maliyeti)

Cevabı `.env` dosyasına yaz:
```
WATCH_DETAIL=balanced
```
Sonra `SETUP_COMPLETE=true` ekle. Bu soruyu bir daha sorma.

Sonraki çağrılarda sessiz kontrol yeterli:
```bash
python "${SKILL_DIR}/scripts/setup.py" --check
```
Exit 0 = hazır, devam et. Başka çıkışta `setup.py` çalıştır.

## Ne Zaman Kullanılır

- Kullanıcı video URL'si yapıştırdığında (YouTube, Vimeo, X, TikTok, Twitch clip, yt-dlp destekli siteler)
- Kullanıcı lokal video dosyası gösterdiğinde (`.mp4`, `.mov`, `.mkv`, `.webm`, vb.)
- Kullanıcı `/watch-brainlab <url-veya-dosya> [soru]` yazdığında

## Nasıl Kullanılır

### Adım 1 — Girdiyi ayrıştır

Video kaynağını (URL veya dosya yolu) ve soruyu ayır.

Örnek: `/watch-brainlab https://youtu.be/abc bu hangi dilde?` → source = `https://youtu.be/abc`, soru = `bu hangi dilde?`

### Adım 2 — Script'i çalıştır

```bash
python "${SKILL_DIR}/scripts/brainwatch.py" "<source>"
```

Opsiyonel flag'ler:
- `--detail transcript|efficient|balanced|token-burner` — detay modu
- `--start T` / `--end T` — belirli bir bölüme odaklan (SS, MM:SS, HH:MM:SS)
- `--timestamps T1,T2,…` — transkriptte "buraya bakın" anlarında frame al
- `--max-frames N` — frame limitini geçersiz kıl
- `--resolution W` — frame genişliği (varsayılan 512, ekran metni okumak için 1024)
- `--fps F` — otomatik fps'i geçersiz kıl (maks 2 fps)
- `--out-dir DIR` — çalışma dizini belirle
- `--whisper groq|openai` — belirli Whisper backend'i zorla
- `--no-whisper` — Whisper fallback'i devre dışı bırak
- `--no-dedup` — benzer frame'leri kaldırma

#### Bölüme Odaklanma

Kullanıcı "2. dakikada ne oluyor?", "0:45 ile 1:00 arasını göster" dediğinde `--start` / `--end` kullan. Odak modunda frame yoğunluğu otomatik artar:

```bash
python "${SKILL_DIR}/scripts/brainwatch.py" "$URL" --start 2:15 --end 2:45
```

Transcript otomatik olarak aynı aralığa filtrelenir.

### Adım 3 — Frame'leri oku

Script'in listelediği her frame dosyasını dosya okuma aracınızla açın. Frame'ler kronolojik sıradadır, `t=MM:SS` zaman damgası videonun hangi anına denk geldiğini gösterir.

### Adım 4 — Cevapla

İki veri akışınız var:
- **Frame'ler** — her zaman damgasında ekranda ne var
- **Transkript** — her zaman damgasında ne söyleniyor

Kullanıcı soru sorduysa, zaman damgaları ile doğrudan cevapla. Soru sormadıysa, videoyu özetle: yapı, önemli anlar, dikkat çekici görseller, konuşulan içerik.

`transcript` detayında bile **özet üret** — ham transkripti yapıştırma. Kullanıcı açıkça isterse ham metni sun.

### Adım 5 — Temizlik

Kullanıcının video hakkında takip sorusu sorma ihtimali yoksa çalışma dizinini sil. Sorabilecekse yerinde bırak.

## Detail Modları

| Mod | Frame'ler | Maks Frame | Açıklama |
|---|---|---|---|
| `transcript` | Yok | — | Sadece transkript. Altyazı varsa video indirmeden çalışır. |
| `efficient` | Keyframe (I-frame) | 50 | Hızlı, neredeyse anlık. Codec sahne kesimlerine yakın. |
| `balanced` | Sahne-algılayan | 100 | Varsayılan. Sahne değişimlerini algılar, statik videoda uniform fallback. |
| `token-burner` | Sahne-algılayan | Limitsiz | Maksimum doğruluk. 250+ frame'de uyarı verir. |

Frame bütçesi videoya süresine göre otomatik ayarlanır:
- ≤30s → ~12-30 frame
- 30s-1dk → ~40 frame
- 1-3dk → ~60 frame
- 3-10dk → ~80 frame
- >10dk → detail modunun limiti, seyrek

Uzun videolarda kullanıcıya belirli bir bölüm isteyip istemediğini sorun.

## Transkripsiyon

1. **Doğal altyazılar (ücretsiz, tercih edilen)** — yt-dlp platformdan manuel veya otomatik altyazı çeker.
2. **Whisper API fallback** — altyazı yoksa ses çıkarılıp API'ye gönderilir:
   - **Groq** (tercih edilen — ucuz, hızlı) — `whisper-large-v3`
   - **OpenAI** (yedek) — `whisper-1`

Anahtarlar `~/.config/brainwatch/.env` dosyasında.

## Hata Durumları

| Durum | Aksiyon |
|---|---|
| Setup başarısız | `setup.py` çalıştır |
| Transkript yok | Frame ile devam et, kullanıcıya söyle |
| Uzun video uyarısı | `--start/--end` ile bölüme odaklanmayı öner |
| İndirme başarısız | yt-dlp hatasını oku, giriş gerektiren/bölge kilitli videolarda kullanıcıya bildir |
| Whisper başarısız | `--whisper openai` veya `--whisper groq` ile diğerini dene |

## Token Verimliliği

Frame'ler ana token maliyetidir. 512px genişlikte 80 frame ≈ 50-80k image token. `--resolution 1024` bunu ~4x artırır, sadece gerektiğinde kullan.

Aynı oturumda bir videoyu zaten izlediyseniz ve takip sorusu gelirse, script'i tekrar çalıştırmayın — bağlamdaki frame ve transkriptten cevap verin.

## Güvenlik

- yt-dlp sadece herkese açık veriyi indirir (giriş/çerez/paylaşım yok)
- ffmpeg lokal çalışır, hiçbir yere veri göndermez
- Sadece çıkarılan ses Whisper API'ye gönderilir (ve yalnızca altyazı yoksa)
- API anahtarları sadece kendi sağlayıcısına gider (Groq → groq.com, OpenAI → openai.com)
- Çalışma dizini ve `~/.config/brainwatch/.env` dışında hiçbir yere yazılmaz

**Script'ler:** `scripts/brainwatch.py` (giriş noktası), `scripts/media.py` (indirme + frame çıkarma), `scripts/transcript.py` (altyazı + Whisper), `scripts/setup.py` (kurulum)
