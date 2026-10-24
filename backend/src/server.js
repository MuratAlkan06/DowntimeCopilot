import express from "express";
import cors from "cors";
import { mockPatients } from "./data/mockPatients.js";
import { checkMedicationSafety, getUpcomingDosesList } from "./utils/medSafetyCheck.js";

const app = express();
const PORT = process.env.PORT || 3001;

app.use(cors());
app.use(express.json());

// In-memory storage for downtime notes (in production: database)
let downtimeNotes = [];
let noteIdCounter = 1;

// ============ PATIENT ROUTES ============

// Get all patients
app.get("/api/patients", (req, res) => {
  res.json({
    success: true,
    data: mockPatients.map(p => ({
      id: p.id,
      name: p.name,
      age: p.age,
      snapshotTime: p.snapshotTime
    }))
  });
});

// Get patient details
app.get("/api/patients/:id", (req, res) => {
  const patient = mockPatients.find(p => p.id === req.params.id);
  if (!patient) {
    return res.status(404).json({ success: false, error: "Patient not found" });
  }
  res.json({ success: true, data: patient });
});

// ============ MEDICATION SAFETY ============

// Check medication safety for a patient
app.post("/api/med-safety-check", (req, res) => {
  const { patientId, medication } = req.body;

  if (!patientId || !medication) {
    return res.status(400).json({ success: false, error: "patientId and medication required" });
  }

  const patient = mockPatients.find(p => p.id === patientId);
  if (!patient) {
    return res.status(404).json({ success: false, error: "Patient not found" });
  }

  const result = checkMedicationSafety(patient, medication);
  res.json({ success: true, data: result });
});

// Get upcoming doses (next 30 min)
app.get("/api/upcoming-doses", (req, res) => {
  const upcoming = getUpcomingDosesList(mockPatients);
  res.json({ success: true, data: upcoming });
});

// ============ DOWNTIME NOTES ============

// Create a downtime note
app.post("/api/downtime-notes", (req, res) => {
  const { patientId, noteType, content, source } = req.body;

  if (!patientId || !noteType || !content) {
    return res.status(400).json({ success: false, error: "patientId, noteType, and content required" });
  }

  const note = {
    id: `note-${noteIdCounter++}`,
    patientId,
    noteType, // e.g., "medication", "observation", "handoff"
    content,
    source: source || "manual", // "manual", "voice", "form_scan"
    timestamp: new Date().toISOString(),
    reviewStatus: "needs_review", // needs_review, approved, discarded
    createdBy: req.body.createdBy || "staff"
  };

  downtimeNotes.push(note);
  res.status(201).json({ success: true, data: note });
});

// Get all downtime notes (recovery queue)
app.get("/api/downtime-notes", (req, res) => {
  const { patientId, status } = req.query;

  let filtered = downtimeNotes;

  if (patientId) {
    filtered = filtered.filter(n => n.patientId === patientId);
  }

  if (status) {
    filtered = filtered.filter(n => n.reviewStatus === status);
  }

  res.json({
    success: true,
    data: filtered,
    counts: {
      total: filtered.length,
      needsReview: filtered.filter(n => n.reviewStatus === "needs_review").length,
      approved: filtered.filter(n => n.reviewStatus === "approved").length
    }
  });
});

// Update note review status
app.patch("/api/downtime-notes/:id", (req, res) => {
  const { reviewStatus } = req.body;
  const note = downtimeNotes.find(n => n.id === req.params.id);

  if (!note) {
    return res.status(404).json({ success: false, error: "Note not found" });
  }

  if (!["needs_review", "approved", "discarded"].includes(reviewStatus)) {
    return res.status(400).json({ success: false, error: "Invalid reviewStatus" });
  }

  note.reviewStatus = reviewStatus;
  note.reviewedAt = new Date().toISOString();

  res.json({ success: true, data: note });
});

// ============ HEALTH CHECK ============

app.get("/health", (req, res) => {
  res.json({ status: "ok", timestamp: new Date().toISOString() });
});

// Start server
app.listen(PORT, () => {
  console.log(`✅ Downtime Copilot backend running on http://localhost:${PORT}`);
  console.log(`   GET  /api/patients - List all patients`);
  console.log(`   GET  /api/patients/:id - Get patient details`);
  console.log(`   POST /api/med-safety-check - Check med safety`);
  console.log(`   GET  /api/upcoming-doses - Next doses due`);
  console.log(`   POST /api/downtime-notes - Create downtime note`);
  console.log(`   GET  /api/downtime-notes - Recovery queue`);
});
