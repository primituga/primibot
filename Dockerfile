# Usa uma versão leve do Python
FROM python:3.11-slim

# Define a pasta de trabalho dentro do container
WORKDIR /app

# Copia apenas o ficheiro de requisitos primeiro (para aproveitar o cache do Docker)
COPY requirements.txt .

# Instala as bibliotecas de uma vez por todas
RUN pip install --no-cache-dir -r requirements.txt

# Copia o resto do código
COPY . .

# Comando para iniciar o bot
CMD ["python", "agent.py"]
