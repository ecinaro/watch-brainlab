# 🎬 WatchBrain Lab

**WatchBrain Lab**, yapay zeka ajanları (Claude Code, Codex, Antigravity, Cursor vb.) için geliştirilmiş gelişmiş bir video analiz skill'idir. 

Verilen bir video bağlantısını (YouTube, Instagram Reels, TikTok, X, Vimeo vb.) veya yerel bir video dosyasını indirir, sahne geçişlerini algılayarak görüntü kareleri (frames) çıkarır, ses kayıtlarını metne (transkript) dönüştürür ve ajanın videoyu saniye saniye analiz etmesini sağlar.

---

## ⚡ Hızlı Kurulum

Ajanik sisteminize göre aşağıdaki tek satırlık komutlardan birini terminalinize yapıştırarak kurabilirsiniz:

### 1. Claude Code Global Kurulum (`~/.claude/skills`)

**macOS / Linux / Git Bash:**
```bash
mkdir -p ~/.claude/skills && cd ~/.claude/skills && (git clone https://github.com/ecinaro/watch-brainlab.git 2>/dev/null || (cd watch-brainlab && git pull origin main))
```

**Windows PowerShell:**
```powershell
$p = "$env:USERPROFILE\.claude\skills"; if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p -Force }; cd $p; if (Test-Path "watch-brainlab") { cd watch-brainlab; git pull origin main } else { git clone https://github.com/ecinaro/watch-brainlab.git }
```

### 2. Her Hangi Bir Proje Klasöründe (`.agents/skills`)

```bash
mkdir -p .agents/skills && cd .agents/skills && (git clone https://github.com/ecinaro/watch-brainlab.git 2>/dev/null || (cd watch-brainlab && git pull origin main))
```

---

## 🛠️ Sistem Gereksinimleri

WatchBrain Lab, ek bir Python paketi (`pip install`) gerektirmez (saf Python stdlib kullanır). Sadece şu 2 sistem aracına ihtiyaç duyar:

1. **`yt-dlp`** (Videoları indirmek için)
2. **`ffmpeg`** & **`ffprobe`** (Kareleri çıkarmak ve ses işlemek için)

### Otomatik Kurulum Kontrolü
Ajan ilk çalıştığında bağımlılıkları otomatik kontrol eder. Eksikse şu komutları önerir:
* **macOS:** `brew install ffmpeg yt-dlp`
* **Windows:** `winget install Gyan.FFmpeg` ve `winget install yt-dlp.yt-dlp`
* **Linux:** `sudo apt install ffmpeg` ve `pip install yt-dlp`

---

## 🚀 Nasıl Kullanılır?

Ajan sohbet ekranında `/watch-brainlab` komutunu video bağlantısı ile çağırmanız yeterlidir:

```text
/watch-brainlab https://www.instagram.com/reel/Da81n4gOK32/ bu videoda ne anlatılıyor?
```

```text
/watch-brainlab C:/Kullanicilar/Videos/ornek_video.mp4
```

### Öne Çıkan Seçenekler (Flag'ler)
* **Bölüme Odaklanma:** `/watch-brainlab <url> --start 0:45 --end 1:15` *(Belirli anlarda kare yoğunluğunu artırır)*
* **Transkript Anlarına Kare Sabitleme:** `/watch-brainlab <url> --timestamps 0:15,0:40`
* **Detay Seviyesi:** `--detail transcript` | `efficient` | `balanced` (varsayılan) | `token-burner`

---

## 🔑 Whisper API Key Yapılandırması (Opsiyonel)

Videonun doğal altyazısı olmadığında (örneğin Instagram Reels veya yerel videolar), konuşmaları metne dönüştürmek için Whisper API kullanılır.

1. **[Groq Console](https://console.groq.com/keys)** adresinden ücretsiz ve hızlı bir `GROQ_API_KEY` alın *(Önerilen)*.
2. Key'i `~/.config/brainwatch/.env` dosyasına kaydedin:

```env
GROQ_API_KEY=gsk_your_key_here
```

*(Not: API Key verilmezse skill durmaz; altyazısız videoları sadece görüntü kareleri (frames-only) olarak analiz etmeye devam eder).*

---

## 📁 Proje Yapısı

```text
watch-brainlab/
├── SKILL.md                 # Yapay zeka ajanı için komut ve davranış rehberi
├── README.md                # Kullanım ve kurulum dokümantasyonu
└── scripts/
    ├── brainwatch.py        # Ana çalışma ve raporlama betiği
    ├── media.py            # Video indirme, ffprobe ve ffmpeg kare çıkarma
    ├── transcript.py       # VTT altyazı ayrıştırma ve Whisper API entegrasyonu
    └── setup.py            # Sistem bağımlılıkları ve preflight kontrolü
```

---

## 👤 Geliştirici & Lisans

* **Yazar:** ecinaro (`@ecinaro.ai`)
* **Topluluk:** BrainLab
* **Lisans:** MIT
