# 🚀 Complete Production Deployment Guide (Railway + WhatsApp Cloud API)

Ye step-by-step guide aapke SellerBot ko **Railway** par deploy karne aur **Meta WhatsApp Cloud API** se live connect karne ke liye banai gayi hai.

---

## 📌 Pehla Faisla: Vercel ya Railway?

> [!IMPORTANT]
> **Railway is recommended over Vercel for this project.**
> - **Vercel** serverless websites ke liye hai (Next.js/React). Ye long-running servers, SSE streams (live chat), aur persistent Redis/Postgres ko directly support nahi karta.
> - **Railway** poore Docker containers, PostgreSQL with `pgvector`, Redis aur FastAPI ko automatically host karta hai aur aapko instant **HTTPS domain** deta hai jo Meta WhatsApp webhook ke liye zaroori hai.

---

## 🛠️ Step 1: Code ko GitHub par Push Karein

1. Apne computer par terminal open karein aur project directory me jayein:
   ```bash
   git init
   git add .
   git commit -m "Production ready SellerBot"
   ```
2. GitHub par new repository banayein (e.g. `sellerbot`) aur push karein:
   ```bash
   git remote add origin https://github.com/YOUR_USERNAME/sellerbot.git
   git branch -M main
   git push -u origin main
   ```

---

## ☁️ Step 2: Railway par Database aur Redis Set Up Karein

1. [Railway.app](https://railway.app/) par account banayein (GitHub se sign in karein).
2. **New Project** par click karein.
3. **Database 1: PostgreSQL**:
   - Click **Add a Service** &rarr; Select **PostgreSQL**.
   - Database create hone ke baad uske **Variables** tab me jayein, aur `DATABASE_URL` copy kar lein.
4. **Database 2: Redis**:
   - Usi project me click **Add a Service** &rarr; Select **Redis**.
   - Redis ke **Variables** tab me jayein aur `REDIS_URL` copy kar lein.

---

## 🚀 Step 3: FastAPI Backend Deploy Karein

1. Usi Railway project me click **Add a Service** &rarr; Select **GitHub Repo**.
2. Apni `sellerbot` repo select karein.
3. Railway automatically `Dockerfile` detect kar ke build shuru kar dega.
4. **Public Domain Generate Karein**:
   - Apni API service ki **Settings** me jayein.
   - **Networking** section me **Generate Domain** par click karein.
   - Aapko ek public HTTPS URL mil jayega, jaise:
     `https://sellerbot-production.up.railway.app`
   *(Ye URL Meta Webhook aur aapke Seller Dashboard dono ke liye use hoga!)*

---

## 🔑 Step 4: Railway Environment Variables (Variables Tab)

Apni API service ke **Variables** tab me jayein aur ye saare attributes add karein:

| Variable Name | Kahan Se Milega / Kya Value Deni Hai |
| :--- | :--- |
| **`DATABASE_URL`** | Railway PostgreSQL service se copy kiya hua connection string. |
| **`REDIS_URL`** | Railway Redis service se copy kiya hua connection string. |
| **`SECRET_KEY`** | Apne terminal me ye command chala kar random 64-char key generate karein:<br>`python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| **`COOKIE_SECURE`** | `true` |
| **`TRUSTED_PROXIES`** | `*` |
| **`EMBEDDING_PROVIDER`** | `gemini` *(ya `openai`)* |
| **`GEMINI_API_KEY`** | [Google AI Studio](https://aistudio.google.com/app/apikey) se free API key generate karein. |
| **`SIMILARITY_THRESHOLD`** | `0.65` |
| **`WHATSAPP_GRAPH_API_VERSION`** | `v21.0` |
| **`WHATSAPP_VERIFY_TOKEN`** | Koi bhi apna secret password/string rakh lein (e.g. `MySuperSecretBotToken2026`). Isko Meta dashboard me paste karna hoga. |
| **`WHATSAPP_APP_SECRET`** | Meta Developer Portal &rarr; App Settings &rarr; Basic &rarr; **App Secret** (Show par click kar ke copy karein). |
| **`WHATSAPP_ACCESS_TOKEN_KEY`** | Ye command chala kar Fernet key generate karein:<br>`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| **`PORT`** | `8000` |

---

## 🗄️ Step 5: Database Migrations Run Karein

Railway me `docker-compose` ka startup script humne already set kar diya hai jo container start hote hi `alembic upgrade head` run karta hai. 

Lekin agar manually verify karna ho:
1. Railway me API service par click karein.
2. Top bar me **Deployments** &rarr; 3 dots par click kar ke **View Logs** check karein.
3. Agar tables create karni hon, **Settings** me **Deploy Command** ko ye set kar sakte hain:
   ```bash
   alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'
   ```

---

## 📱 Step 6: Meta WhatsApp Cloud API Connect Karein

1. **Meta Developer Portal par Jayein**:
   - [developers.facebook.com](https://developers.facebook.com/) par login karein.
   - **Create App** &rarr; Select Type **Other** &rarr; Next &rarr; Select **Business** &rarr; Create App.
2. **WhatsApp Product Add Karein**:
   - Dashboard me scroll down karein aur **WhatsApp** ke niche **Set Up** click karein.
3. **Configure Webhook**:
   - Left menu me **WhatsApp** &rarr; **Configuration** par jayein.
   - **Edit** Webhook URL par click karein:
     - **Callback URL:** `https://your-app-production.up.railway.app/webhooks/whatsapp`
     - **Verify Token:** Jo aapne `WHATSAPP_VERIFY_TOKEN` me rakha tha (e.g. `MySuperSecretBotToken2026`).
     - Click **Verify and Save** ✅.
   - **Webhook Fields**:
     - Niche list me **`messages`** dhoondein aur **Subscribe** par click karein.
4. **Phone Number & Permanent Token**:
   - **API Setup** tab me jayein:
     - Wahan aapko **Phone Number ID** mil jayega (copy kar lein).
     - Temporary access token ko test ke liye use kar sakte hain, ya **System User** bana kar permanent access token generate karein.

---

## 🎯 Step 7: Live Test Karein

1. Apne browser me Railway ka URL open karein:
   `https://your-app-production.up.railway.app/`
2. **Sign Up Karein**:
   - Store name, email aur password daal kar seller account banayein.
3. **Catalog & FAQs Add Karein**:
   - **Products** tab me CSV upload karein (template download kar ke).
   - **FAQs** tab me 2-3 common sawal daalein (e.g. Delivery charges, return policy).
4. **WhatsApp Connect Karein**:
   - **WhatsApp setup** tab me jayein.
   - Meta se copy kiya hua **Phone Number ID** aur **Access Token** paste karein aur **Save** karein.
5. **Mobile se Message Bhejein**:
   - Apne WhatsApp se us test number par koi product ka naam bhejein (e.g. *"Neeli kurta ka price kya hai?"*).
   - Bot instantly database se stock aur price nikaal kar reply karega!
   - Seller dashboard ke **Conversations** tab me live chat show ho jayegi!
