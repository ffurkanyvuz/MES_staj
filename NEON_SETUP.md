# Neon PostgreSQL kurulumu

1. [Neon](https://neon.com) hesabı oluşturun ve yeni bir PostgreSQL projesi açın.
2. Paneldeki **Connection string** değerini kopyalayın. `sslmode=require` bölümü korunmalıdır.
3. Kalıcı Streamlit bağlantısı için `.streamlit/secrets.toml` dosyasını oluşturun:

   ```toml
   DATABASE_URL = "Neon bağlantı adresiniz"
   PUBLIC_APP_URL = "Herkese açık Streamlit adresiniz"
   ```

   Bu dosya `.gitignore` içinde olduğu için kaynak kod deposuna eklenmez.

4. Veriler henüz taşınmadıysa Windows PowerShell'de proje klasöründe aşağıdaki komutu çalıştırın:

   ```powershell
   $env:DATABASE_URL='Neon bağlantı adresiniz'
   .\venv\Scripts\pip.exe install -r requirements.txt
   .\venv\Scripts\python.exe .\migrate_to_postgres.py
   ```

5. Uygulamayı başlatın:

   ```powershell
   .\venv\Scripts\streamlit.exe run app.py
   ```

Uygulama önce `DATABASE_URL` ortam değişkenini, bu bulunamazsa `.streamlit/secrets.toml` dosyasını okur. Secrets dosyası kullanıldığında yeni terminal açınca bağlantıyı tekrar tanımlamanız gerekmez.

`DATABASE_URL` değerini sohbet, ekran görüntüsü veya kaynak koda koymayın; bu değer veritabanı parolasını içerir.
