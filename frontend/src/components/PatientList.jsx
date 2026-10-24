import { useState, useEffect } from 'react';
import { patients } from '../api';
import './PatientList.css';

function PatientList({ onSelectPatient, isOffline }) {
  const [patientList, setPatientList] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (isOffline) {
      setLoading(false);
      return;
    }

    patients.list()
      .then(res => {
        setPatientList(res.data.data);
        setLoading(false);
      })
      .catch(err => {
        setError(err.message);
        setLoading(false);
      });
  }, [isOffline]);

  if (loading) return <div className="loading">Loading patients...</div>;

  if (error && !isOffline) {
    return <div className="error">Error: {error}</div>;
  }

  if (isOffline) {
    return (
      <div className="offline-message">
        <p>📡 Running offline - using cached patient data</p>
        <p className="offline-hint">In a real outage, you'd see last-synced patients here</p>
      </div>
    );
  }

  return (
    <div className="patient-list">
      <h2>Patients on Unit (24-bed med-surg)</h2>
      <div className="patient-cards">
        {patientList.map(patient => (
          <div
            key={patient.id}
            className="patient-card"
            onClick={() => onSelectPatient(patient.id)}
          >
            <div className="patient-name">{patient.name}</div>
            <div className="patient-meta">
              <span>Age {patient.age}</span>
              <span className="snapshot-time">
                📸 Snapshot: {new Date(patient.snapshotTime).toLocaleTimeString()}
              </span>
            </div>
            <button className="view-btn">View Details →</button>
          </div>
        ))}
      </div>
    </div>
  );
}

export default PatientList;
