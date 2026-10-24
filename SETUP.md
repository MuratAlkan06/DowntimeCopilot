# Getting Started - Downtime Copilot

## Quick Start

### Prerequisites
- Node.js 18+ and npm/yarn
- Two terminal windows

### 1. Install Dependencies

**Backend:**
```bash
cd backend
npm install
```

**Frontend:**
```bash
cd frontend
npm install
```

### 2. Start the Backend (Terminal 1)

```bash
cd backend
npm run dev
```

You should see:
```
✅ Downtime Copilot backend running on http://localhost:3001
```

### 3. Start the Frontend (Terminal 2)

```bash
cd frontend
npm run dev
```

You should see:
```
✨ Vite server running at http://localhost:3000
```

### 4. Open in Browser

Go to **http://localhost:3000** and start exploring!

---

## Project Structure

```
DowntimeCopilot/
├── backend/
│   ├── src/
│   │   ├── server.js              # Express API
│   │   ├── data/
│   │   │   └── mockPatients.js    # Synthetic patient data
│   │   └── utils/
│   │       └── medSafetyCheck.js  # Local medication safety rules
│   └── package.json
│
├── frontend/
│   ├── src/
│   │   ├── App.jsx                # Main app
│   │   ├── api.js                 # API client
│   │   └── components/
│   │       ├── PatientList.jsx    # Patient list view
│   │       ├── PatientDetail.jsx  # Patient detail + med safety + notes
│   │       └── RecoveryQueue.jsx  # Recovery queue after outage
│   ├── index.html
│   └── package.json
│
└── README.md
```

---

## What to Build Next

### Person 1: Frontend & UX
- ✅ Patient list, patient details, med entry form
- [ ] Improve styling/design
- [ ] Add handoff card view
- [ ] Form photograph upload UI (for Textract integration)
- [ ] Voice order capture UI (for Transcribe integration)

### Person 2: Backend & Data
- ✅ Express API with mock data
- ✅ Local med safety rules
- [ ] Replace mock data with real Synthea/FHIR generation
- [ ] Add database (MongoDB, PostgreSQL, SQLite)
- [ ] Add authentication/role-based access
- [ ] Add encryption for sensitive data

### Person 3: AI Workflow
- [ ] Integrate Bedrock for handoff summaries
- [ ] Integrate Bedrock for form parsing (with Textract)
- [ ] Error handling when Bedrock is unavailable
- [ ] Add policy RAG (Bedrock Knowledge Bases)
- [ ] Handoff card template & formatting

### Person 4: AWS & Infrastructure
- [ ] Set up Bedrock in AWS account
- [ ] Lambda functions for API calls
- [ ] S3 for encrypted snapshot storage
- [ ] DynamoDB for audit logs
- [ ] KMS for encryption keys

---

## API Endpoints

### Patients
- `GET /api/patients` — List all patients
- `GET /api/patients/:id` — Get patient details

### Medication Safety
- `POST /api/med-safety-check` — Check if a med is safe
  - Request: `{ patientId, medication }`
  - Response: `{ alerts, isSafe, snapshotTime }`
- `GET /api/upcoming-doses` — Next 30 min of due meds

### Downtime Notes (Recovery Queue)
- `POST /api/downtime-notes` — Create a note
  - Request: `{ patientId, noteType, content, source }`
  - Response: `{ id, timestamp, reviewStatus }`
- `GET /api/downtime-notes` — List notes (filter by status)
- `PATCH /api/downtime-notes/:id` — Update review status

---

## Next Steps

1. **Design** — Agree on app framework and data shapes
2. **Mock first** — Everyone builds with mock data
3. **Integrate early** — Connect the pieces on day 2
4. **Real AWS** — Swap mock Bedrock for real calls
5. **Polish** — Test the demo flow end-to-end

Good luck! 🚀
