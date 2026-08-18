.PHONY: help db frontend all

help:
	@echo "Usage:"
	@echo "  make db       Start scenario-db-service"
	@echo "  make frontend Start scenario-frontend"
	@echo "  make all      Start both services in one command"

# Backend API + Postgres
# Requires .env to exist in services/scenario-db-service
# Copy .env.example to .env first if needed.
db:
	cd services/scenario-db-service && docker compose up --build

# Vite frontend
# Requires .env to exist in services/scenario-frontend
# Copy .env.example to .env first if needed.
frontend:
	cd services/scenario-frontend && npm install && npm run dev

# Start both services together in one shell.
# This keeps both processes attached to the same terminal, so Ctrl+C stops both.
all:
	@echo "Starting scenario-db-service and scenario-frontend..."
	(cd services/scenario-db-service && docker compose up --build) & \
	(cd services/scenario-frontend && npm install && npm run dev) & \
	wait
