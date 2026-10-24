import { useState, useEffect } from 'react';
import { patients, medSafety, downtimeNotes } from '../api';
import './PatientDetail.css';

function PatientDetail({ patientId, onBack, isOffline }) {
  const [patient, setPatient] = useState(null);
  const [loading, setLoading] = useState(true);
  const [medInput, setMedInput] = useState('');
  const [safetyResult, setSafetyResult] = useState(null);
  const [noteInput, setNoteInput] = useState('');
  const [notes, setNotes] = useState([]);

  useEffect(() => {
    if (!patientId || isOffline) {
      setLoading(false);
      return;
    }

    patients.get(patientId)
      .then(res => {
        setPatient(res.data.data);
        setLoading(false);
      })
      .catch(err => {
        console.error('Error loading patient:', err);
        setLoading(false);
      });
  }, [patientId, isOffline]);

  useEffect(() => {
    if (patientId && !isOffline) {
      downtimeNotes.list(patientId)
        .then(res => setNotes(res.data.data))
        .catch(err => console.error('Error loading notes:', err));
    }
  }, [patientId, isOffline]);

  const handleCheckMedSafety = async () => {
    if (!medInput.trim()) return;

    try {
      const result = await medSafety.check(patientId, medInput);
      setSafetyResult(result.data);
    } catch (err) {
      console.error('Error checking med safety:', err);
    }
  };

  const handleAddNote = async () => {
    if (!noteInput.trim()) return;

    try {
      const result = await downtimeNotes.create(patientId, 'medication', noteInput, 'manual');
      setNotes([...notes, result.data]);
      setNoteInput('');
    } catch (err) {
      console.error('Error adding note:', err);
    }
  };

  if (loading) return <div className="loading">Loading patient...</div>;

  if (isOffline && !patient) {
    return (
      <div className="patient-detail">
        <button className="back-btn" onClick={onBack}>← Back to Patients</button>
        <div className="offline-message">
          <p>📡 Running offline - cached patient data unavailable</p>
          <p>In a real outage, you'd see the last snapshot here</p>
        </div>
      </div>
    );
  }

  if (!patient) {
    return (
      <div className="patient-detail">
        <button className="back-btn" onClick={onBack}>← Back to Patients</button>
        <div className="error">Patient not found</div>
      </div>
    );
  }

  return (
    <div className="patient-detail">
      <button className="back-btn" onClick={onBack}>← Back to Patients</button>

      <div className="patient-header">
        <h2>{patient.name}</h2>
        <div className="snapshot-info">
          📸 Snapshot at {new Date(patient.snapshotTime).toLocaleTimeString()}
        </div>
      </div>

      <div className="detail-grid">
        {/* Allergies */}
        <div className="card allergies-card">
          <h3>⚠️ Allergies</h3>
          {patient.allergies.length > 0 ? (
            <ul>
              {patient.allergies.map((allergy, i) => (
                <li key={i} className="allergy-item">{allergy.name}</li>
              ))}
            </ul>
          ) : (
            <p className="no-data">No known allergies</p>
          )}
        </div>

        {/* Current Medications */}
        <div className="card medications-card">
          <h3>💊 Current Medications</h3>
          {patient.currentMedications.length > 0 ? (
            <ul>
              {patient.currentMedications.map((med, i) => (
                <li key={i} className="med-item">
                  <strong>{med.name}</strong> {med.dose}
                  <span className="frequency">{med.frequency}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="no-data">No medications</p>
          )}
        </div>

        {/* Upcoming Doses */}
        <div className="card doses-card">
          <h3>⏰ Upcoming Doses (Next 4 Hours)</h3>
          {patient.upcomingDoses.length > 0 ? (
            <ul>
              {patient.upcomingDoses.map((dose, i) => (
                <li key={i} className="dose-item">
                  <strong>{dose.medication}</strong>
                  <span className="due-time">
                    Due: {new Date(dose.dueAt).toLocaleTimeString()}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="no-data">No upcoming doses</p>
          )}
        </div>

        {/* Recent Labs */}
        <div className="card labs-card">
          <h3>🧪 Recent Labs</h3>
          {patient.recentLabs.length > 0 ? (
            <ul>
              {patient.recentLabs.map((lab, i) => (
                <li key={i} className="lab-item">
                  <strong>{lab.test}:</strong> {lab.value}
                  <span className="range">({lab.range})</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="no-data">No recent labs</p>
          )}
        </div>
      </div>

      {/* Medication Safety Check */}
      <div className="card med-check-card">
        <h3>🛡️ Check Medication Safety</h3>
        <div className="med-input-group">
          <input
            type="text"
            placeholder="Enter medication to check..."
            value={medInput}
            onChange={(e) => setMedInput(e.target.value)}
            onKeyPress={(e) => e.key === 'Enter' && handleCheckMedSafety()}
          />
          <button onClick={handleCheckMedSafety} disabled={isOffline}>
            Check
          </button>
        </div>

        {safetyResult && (
          <div className={`safety-result ${safetyResult.isSafe ? 'safe' : 'unsafe'}`}>
            <div className="result-header">
              {safetyResult.isSafe ? '✅ Safe to administer' : '⚠️ Safety Alerts'}
            </div>
            {safetyResult.alerts.map((alert, i) => (
              <div key={i} className={`alert alert-${alert.severity}`}>
                <strong>{alert.type}</strong>
                <p>{alert.message}</p>
                <small className="source">{alert.source}</small>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Capture Downtime Note */}
      {!isOffline && (
        <div className="card note-capture-card">
          <h3>📝 Capture Downtime Note</h3>
          <textarea
            placeholder="Document what you're giving, observing, or any changes..."
            value={noteInput}
            onChange={(e) => setNoteInput(e.target.value)}
            rows="4"
          />
          <button onClick={handleAddNote}>Add Note to Queue</button>

          {notes.length > 0 && (
            <div className="notes-list">
              <h4>Notes Captured ({notes.length})</h4>
              {notes.map((note, i) => (
                <div key={i} className="note-item">
                  <small className="note-type">{note.noteType}</small>
                  <p>{note.content}</p>
                  <small className="note-time">
                    {new Date(note.timestamp).toLocaleTimeString()}
                  </small>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default PatientDetail;
