# 🚀 PrimiBot - Hybrid AI Assistant for Twitch and Discord

![Python](https://img.shields.io/badge/Python-3.11-blue?style=for-the-badge&logo=python)
![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?style=for-the-badge&logo=docker)
![Discord](https://img.shields.io/badge/Discord-Bot-5865F2?style=for-the-badge&logo=discord)
![Twitch](https://img.shields.io/badge/Twitch-Bot-9146FF?style=for-the-badge&logo=twitch)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)

**PrimiBot** is a next-generation AI assistant designed to operate simultaneously on **Twitch** and **Discord**. 

Its core innovation is the **Hybrid Fallback Architecture ("The Parachute")**, which prioritizes generating text locally for free (using your own GPU), but automatically triggers Cloud APIs (Groq) if the local hardware is overloaded or taking too long. This ensures your chat *never* goes unanswered.

---

## ✨ Key Features

* 🧠 **Fallback Architecture (Local ➔ Cloud):** Attempts to answer using local LLMs via **Ollama / Flowise** first. If the local PC doesn't respond within 45 seconds, it instantly falls back to the lightning-fast **Groq API** to guarantee message delivery.
* 🌐 **Real-Time Web Context:** Integrated with **SearXNG** to search the web before answering, ensuring up-to-date and factually accurate responses.
* 💾 **Persistent Memory:** Uses **Redis** to remember the last 14 messages of each user contextually (with automatic 24h expiration to save space).
* 📝 **Smart Formatting:** Automatically splits long Discord messages to bypass the 2000-character limit without cutting words, and adapts to Twitch's strict 480-character limit.
* 🐳 **100% Dockerized:** Optimized to run on lightweight servers (like a Raspberry Pi 5) or robust PCs with NVIDIA GPUs.

---

## 🏗️ How the Architecture Works

1. **Local Attempt:** A user sends `!ai how do you bake a cake?`. The bot asks your local machine (e.g., an RTX 3080 running Llama 3.1) to generate the answer.
2. **The Timeout:** The bot waits exactly 45 seconds.
3. **The Fallback:** If your GPU is busy (e.g., you are playing a heavy game), the bot cancels the local request and asks the Cloud (Groq), delivering the response in under a second.

---

## 📋 Prerequisites

To run this bot, you will need:
* [Docker](https://www.docker.com/) and Docker Compose installed.
* A server or Raspberry Pi to keep the containers online.
* Bot Tokens from [Discord](https://discord.com/developers/applications) and [Twitch](https://twitchapps.com/tmi/).
* A free API key from [Groq](https://console.groq.com/keys).

---

## 🚀 How to Install and Run

**1. Clone the repository:**
```bash
git clone [https://github.com/YOUR_USERNAME/primibot.git](https://github.com/YOUR_USERNAME/primibot.git)
cd primibot
```

**2. Configure Keys and Tokens:**
Make a copy of the example environment file and fill in your real keys:
```bash
cp .env.example .env
```
*(Open the `.env` file in your text editor and place your Discord, Twitch, and Groq tokens).*

**3. Start the Engines:**
Let Docker build the image and start the entire infrastructure in the background:
```bash
docker-compose up -d --build
```

**4. Check the Logs:**
To ensure the bot woke up and connected to the platforms:
```bash
docker logs -f langchain-agent
```

---

## 💬 Available Commands

| Command | Platform | Description |
| :--- | :--- | :--- |
| `!ai <question>` | Twitch & Discord | Asks the Artificial Intelligence a question. |
| `!ai_reset` | Twitch & Discord | Clears the current conversation memory (useful to change topics). |

---

## 📂 Project Structure

```text
primibot/
├── docker-compose.yml       # Infrastructure orchestrator (Ollama, Redis, Bot)
├── .env.example             # Environment variables template
└── langchain-agent/
    ├── agent.py             # The bot's brain in Python
    ├── Dockerfile           # Image build instructions
    └── requirements.txt     # Python dependencies
```

---

## 🤝 Contributing

Contributions are always welcome! If you want to improve the fallback logic, add support for new platforms (like YouTube Live), or optimize the code:
1. Fork the Project.
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`).
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`).
4. Push to the Branch (`git push origin feature/AmazingFeature`).
5. Open a Pull Request.

---

## ⚖️ License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.