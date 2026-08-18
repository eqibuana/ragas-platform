# ragas-platform

RAGAS-based quality evaluation for PermataBank AI Platform's RAG pipelines (People Care / HR agent and Contact Center agent). It drives the deployed chat API with a golden question set, then scores every answer with an LLM judge across faithfulness, relevancy, context precision/recall, correctness, noise sensitivity, and summarization quality.

The platform includes a web-based UI for managing evaluation scenarios, running RAGAS evaluations, comparing runs, and viewing audit logs.

## Repository layout

```
ragas-platform/
  services/
    ragas-service/                 # the evaluation engine — see its README for full details
      ├── ragas_evaluation.py
      ├── ragas_data.xlsx          # golden dataset (HR + Contact Center sheets)
      ├── .env.example             # AWS credentials template
      ├── Dockerfile
      ├── docker-compose.yml
      └── output/                  # generated results + run logs (gitignored)
    
    scenario-db-service/           # Backend API for scenario management (NEW)
      ├── app/
      │   ├── main.py              # FastAPI application
      │   ├── models.py            # Database models
      │   ├── database.py          # PostgreSQL connection & ORM
      │   ├── routers/             # API endpoints (datasets, runs, audit, auth)
      │   ├── ragas_runner.py      # Integration with ragas-service
      │   └── ...
      ├── .env.example             # Configuration template
      ├── requirements.txt
      ├── Dockerfile
      └── docker-compose.yml
    
    scenario-frontend/             # React/Vite web UI (NEW)
      ├── src/
      │   ├── App.tsx              # Main application component
      │   ├── components/
      │   │   ├── DatasetPicker.tsx        # Dropdown with keyboard nav & scrolling
      │   │   ├── AuditLogPanel.tsx        # Full audit trail with run versions
      │   │   ├── RunCompare.tsx           # Compare two runs side-by-side
      │   │   ├── RunResults.tsx           # View run results & metrics
      │   │   ├── ScenarioTable.tsx        # Edit test scenarios
      │   │   └── UploadForm.tsx           # Upload Excel scenario files
      │   ├── index.css            # Styled with modern design (IMPROVED)
      │   └── ...
      ├── package.json
      ├── tsconfig.json
      ├── vite.config.ts
      ├── Dockerfile
      └── docker-compose.yml
  
  scripts/
    ├── seed.py                     # seeds default roles/admin user into the platform's auth DB
    └── *.xlsx                      # scenario/golden-dataset working files
  
  Makefile                          # Project automation commands
  ragas_env/                        # local Python venv for services/ragas-service (gitignored)
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Frontend (React/Vite/TypeScript)              │
│              Scenario Manager UI @ http://localhost:5173         │
│  - Upload & manage test scenarios (HR, Contact Center domains)  │
│  - Run RAGAS evaluations                                        │
│  - Compare runs side-by-side                                    │
│  - View audit logs with rollback                                │
└───────────────────────┬─────────────────────────────────────────┘
                        │ HTTP/REST
                        ↓
┌─────────────────────────────────────────────────────────────────┐
│               Backend API (FastAPI/Python)                       │
│           Scenario DB Service @ http://localhost:8000            │
│  - Scenario CRUD operations                                     │
│  - Run management & orchestration                               │
│  - Audit logging with versioning                                │
│  - Auth & RBAC enforcement                                      │
└───┬──────────────────────────────────────┬──────────────────────┘
    │ SQL                                  │ Docker API
    ↓                                      ↓
┌──────────────────┐          ┌──────────────────────────────────┐
│   PostgreSQL DB  │          │  RAGAS Service (Docker Container)│
│ - Scenarios      │          │ @ http://ragas-service:8001      │
│ - Runs & Metrics │          │                                  │
│ - Audit Log      │          │ - Run evaluations                │
└──────────────────┘          │ - Score answers (LLM judge)      │
                               │ - Generate reports              │
                               └───────────┬──────────────────────┘
                                           │ AWS Bedrock & SageMaker
                                           ↓
                               ┌──────────────────────────────────┐
                               │    Deployed AI Platform APIs     │
                               │  - HR Agent Chat                 │
                               │  - Contact Center Chat           │
                               └──────────────────────────────────┘
```

---

### Option 1: Full Stack (Recommended)
Run all services with Docker Compose:

```powershell
cd services\scenario-db-service
copy .env.example .env   # fill in your database and AWS credentials
docker compose up --build
```

This starts:
- **Frontend**: Web UI at http://localhost:5173
- **Backend API**: at http://localhost:8000
- **PostgreSQL Database**: for scenario management
- **RAGAS Service**: evaluation engine (triggered via API)

### Option 2: Backend + Evaluation Service Only
If you just want to run RAGAS evaluations without the UI:

```powershell
cd services\ragas-service
copy .env.example .env   # fill in your AWS credentials
docker compose up --build
```

Results are written to `services/ragas-service/output/ragas_results.xlsx`.

To run a single domain:
```powershell
docker compose run --rm ragas-evaluation --domain hr
```

### Option 3: Scenario UI + Database Only
To run the scenario management UI and database without RAGAS:

```powershell
cd services\scenario-db-service
copy .env.example .env
docker compose up --build
```

Then access the UI at http://localhost:5173.

---

## Features

### 🎨 Scenario Manager UI (New)
- **Dataset Management**: Upload Excel files or create scenarios manually
- **Smart Dropdown**: Keyboard navigation (arrow keys, Enter, Escape), scrolls through unlimited datasets
- **Scenario Editor**: Add, edit, delete test scenarios with live validation
- **Run Evaluation**: Trigger RAGAS runs from the UI
- **Compare Runs**: Side-by-side comparison of two evaluation runs with delta metrics
- **Audit Log**: Complete history of changes with version tracking and rollback capability
- **Responsive Design**: Modern, clean UI with smooth animations

### 📊 RAGAS Evaluation Engine
- Multi-metric scoring: Faithfulness, relevancy, context precision/recall, correctness, noise sensitivity, summarization quality
- LLM judge via AWS Bedrock
- Per-domain evaluation (HR + Contact Center)
- Batch processing with detailed logs

### 🔐 Security & RBAC
- Role-based access control (admin, hr_user, cc_user)
- Secure credential management via environment variables
- Domain-specific user permissions

---

## UI Improvements (Session 2-3)

✅ **Dataset Picker Enhancements**
- Dynamic dropdown height: `calc(100vh - 300px)` adapts to viewport
- Keyboard navigation: Arrow keys, Enter, Escape
- Auto-scroll for highlighted items
- Custom scrollbar styling (10px wide, smooth hover effects)
- Touch-friendly targets (44px minimum height)
- Visual feedback: hover, selected, and active states with smooth transitions

✅ **Layout Reorganization**
- Cleaner control sections: Upload → Dataset → Actions
- Section dividers for visual hierarchy
- Better responsive behavior with flex-wrap
- Consistent button styling with hover effects

✅ **Accessibility**
- ARIA labels and semantic roles
- Focus management with visible outlines
- Screen reader support
- Keyboard-only navigation support

✅ **Performance**
- Efficient rendering for large lists (100+)
- Cross-browser compatibility (Chrome, Firefox, Safari, Edge)
- Smooth transitions without jank

## Requirements

### Docker (Recommended)
- Docker Desktop with `docker-compose`
- 2GB+ available RAM
- Network access to deployed AI Platform API

### Local Development
- Python 3.11 (for ragas-service)
- Node.js 18+ (for scenario-frontend)
- PostgreSQL 14+ (for scenario-db-service)
- AWS credentials with access to:
  - **Bedrock** (for judge LLM)
  - **SageMaker** embedding endpoint in `ap-southeast-3`
- Network access to the deployed AI Platform API

## Configuration

### AWS Credentials
Never commit `.env` files with real credentials — they're gitignored. Always use `.env.example` templates:

```powershell
# For RAGAS evaluation
services/ragas-service/.env

# For scenario DB backend
services/scenario-db-service/.env

# For scenario frontend (usually empty unless custom backend URL)
services/scenario-frontend/.env
```

Example `.env.example` contents:
```env
AWS_ACCESS_KEY_ID=your-aws-access-key-id
AWS_SECRET_ACCESS_KEY=your-aws-secret-access-key
AWS_DEFAULT_REGION=ap-southeast-3
POSTGRES_PASSWORD=your-secure-password
```

### Environment Variables

**RAGAS Service** (`services/ragas-service/.env`):
- `AWS_ACCESS_KEY_ID` - AWS access key
- `AWS_SECRET_ACCESS_KEY` - AWS secret key
- `AWS_DEFAULT_REGION` - AWS region (default: ap-southeast-3)

**Database Service** (`services/scenario-db-service/.env`):
- `POSTGRES_PASSWORD` - PostgreSQL password
- `AWS_*` - Same AWS credentials as RAGAS service
- `AI_PLATFORM_URL` - API endpoint of the deployed AI Platform
- `CORS_ORIGINS` - Allowed frontend origins (default: http://localhost:5173)

**Frontend** (`services/scenario-frontend/.env`):
- Optional: Set custom backend API URL if not using default (http://localhost:8000)

---

## API Documentation

Full API documentation available at: **[services/scenario-db-service/README.md](services/scenario-db-service/README.md)**

Key endpoints:
- `GET /datasets` - List scenario sets
- `POST /datasets` - Upload new scenario file
- `GET /runs` - List evaluation runs
- `POST /runs` - Start RAGAS evaluation
- `GET /compare` - Compare two runs
- `GET /audit` - View audit log
- `POST /audit/rollback` - Restore previous row state

---

## Development

### Frontend Development
```powershell
cd services/scenario-frontend
npm install
npm run dev  # starts dev server at http://localhost:5173
```

### Backend Development
```powershell
cd services/scenario-db-service
python -m venv venv
source venv/Scripts/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

### Running Tests
```powershell
cd services/scenario-frontend
npm run test

cd services/scenario-db-service
pytest
```

---

## Troubleshooting

### Push Protection / Secret Scanning Errors
GitHub's push protection blocks commits containing credentials. Always:
1. Use `.env.example` templates without real values
2. Never commit `.env` files (they're gitignored)
3. Remove credentials from example files before committing

### Frontend Not Connecting to Backend
- Check `CORS_ORIGINS` in `services/scenario-db-service/.env`
- Ensure backend is running: `docker compose ps`
- Frontend needs `http://localhost:8000` accessible

### Database Connection Errors
- Verify `POSTGRES_PASSWORD` matches in docker-compose
- Check PostgreSQL container is running: `docker compose logs postgres`
- Ensure port 5432 isn't in use by another service

### AWS Credential Errors
- Verify credentials have Bedrock and SageMaker access
- Check credentials aren't expired
- Confirm region is `ap-southeast-3`
