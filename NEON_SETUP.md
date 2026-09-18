# Neon PostgreSQL kurulumu

1. [Neon](https://neon.com) hesabı oluşturun ve yeni bir PostgreSQL projesi açın.
2. Paneldeki **Connection string** değerini kopyalayın. `sslmode=require` bölümü korunmalıdır.
3. Windows PowerShell'de, proje klasöründe aşağıdaki komutu çalıştırın:

   ```powershell
   $env:DATABASE_URL='Neon bağlantı adresiniz'
   .\venv\Scripts\pip.exe install -r requirements.txt
   .\venv\Scripts\python.exe .\migrate_to_postgres.py
   ```

4. Taşıma tamamlandıktan sonra uygulamayı aynı PowerShell penceresinde başlatın:

   ```powershell
   .\venv\Scripts\streamlit.exe run app.py
   ```

`DATABASE_URL` yalnızca tanımlandığı PowerShell oturumunda geçerlidir. Yeni bir terminal açarsanız uygulamayı başlatmadan önce 3. adımdaki ilk satırı tekrar çalıştırın.

`DATABASE_URL` değerini sohbet, ekran görüntüsü veya kaynak koda koymayın; bu değer veritabanı parolasını içerir.
