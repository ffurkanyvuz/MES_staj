# TREX MES · Ücretsiz Yerel Yapay Zekâ

Bu yapı OpenAI API kullanmaz. Yapay zekâ yalnızca bilgisayar, Ollama, TREX köprüsü ve tünel açıkken çalışır. Bağlantı kapalıysa TREX MES otomatik olarak temel yerel analiz moduna döner.

## Bir defalık kurulum

1. Ollama for Windows uygulamasını kurun.
2. PowerShell'de modeli indirin: `ollama pull qwen2.5:7b`
3. Cloudflared aracını kurun.
4. Uzun ve rastgele bir köprü anahtarı oluşturun. Bu anahtarı GitHub'a yazmayın.

## Her kullanımda (önerilen tek adım)

Proje klasöründe aşağıdaki dosyaya sağ tıklayıp **PowerShell ile çalıştır** seçeneğini kullanın:

`start_trex_ollama.ps1`

Pencerede gösterilen köprü adresi ve geçici bağlantı anahtarını TREX MES içinde **Akıllı Analiz > Yerel AI bağlantısı** alanına girin. Pencere açık kaldığı sürece yapay zekâ çalışır; pencere kapatılınca TREX MES otomatik olarak temel yerel moda döner.

Bu bilgisayarda CUDA uyumsuzluğu görüldüğü için başlatıcı Ollama'yı `cpu_avx2` modunda çalıştırır.

## Elle başlatma

Üç ayrı PowerShell penceresinde çalıştırın:

```powershell
ollama serve
$env:TREX_OLLAMA_TOKEN="UZUN_RASTGELE_GIZLI_ANAHTAR"
.\start_ollama_bridge.ps1
```

```powershell
.\start_ollama_tunnel.ps1
```

Cloudflared çıktısındaki `https://...trycloudflare.com` adresini kopyalayın. Streamlit Cloud Secrets alanındaki değerleri güncelleyin:

```toml
AI_PROVIDER = "ollama"
OLLAMA_BASE_URL = "https://...trycloudflare.com"
OLLAMA_TOKEN = "UZUN_RASTGELE_GIZLI_ANAHTAR"
OLLAMA_MODEL = "qwen2.5:7b"
```

Hızlı tünel adresi her yeniden başlatmada değişebilir. Sabit adres için daha sonra Cloudflare named tunnel ve alan adı yapılandırılabilir.

## Güvenlik

- `OLLAMA_TOKEN` değerini sohbet, GitHub veya ekran görüntüsünde paylaşmayın.
- Ollama'nın `11434` portunu doğrudan internete açmayın.
- İşiniz bitince köprü ve tünel pencerelerini kapatabilirsiniz.

