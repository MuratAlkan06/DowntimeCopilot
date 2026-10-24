import { useState, useEffect } from 'react';
import { patients, medSafety, downtimeNotes } from './api';
import PatientList from './components/PatientList';
import PatientDetail from './components/PatientDetail';
import RecoveryQueue from './components/RecoveryQueue';
import './App.css';

function App() {
  const [currentPage, setCurrentPage] = useState('patients'); // patients, patient-detail, recovery-queue
  const [selectedPatientId, setSelectedPatientId] = useState(null);
  const [isOffline, setIsOffline] = useState(false);

  const handleSelectPatient = (patientId) => {
    setSelectedPatientId(patientId);
    setCurrentPage('patient-detail');
  };

  const handleBack = () => {
    setCurrentPage('patients');
    setSelectedPatientId(null);
  };

  return (
    <div className="app">
      <header className="app-header">
        <div className="header-content">
          <h1>🏥 Downtime Copilot</h1>
          <div className="header-controls">
            <button
              className={`offline-toggle ${isOffline ? 'offline' : 'online'}`}
              onClick={() => setIsOffline(!isOffline)}
            >
              {isOffline ? '📡 Offline' : '✅ Online'}
            </button>
          </div>
        </div>
      </header>

      <nav className="nav-tabs">
        <button
          className={currentPage === 'patients' ? 'active' : ''}
          onClick={() => setCurrentPage('patients')}
        >
          👥 Patients
        </button>
        <button
          className={currentPage === 'recovery-queue' ? 'active' : ''}
          onClick={() => setCurrentPage('recovery-queue')}
        >
          📋 Recovery Queue
        </button>
      </nav>

      <main className="main-content">
        {currentPage === 'patients' && (
          <PatientList onSelectPatient={handleSelectPatient} isOffline={isOffline} />
        )}

        {currentPage === 'patient-detail' && (
          <PatientDetail
            patientId={selectedPatientId}
            onBack={handleBack}
            isOffline={isOffline}
          />
        )}

        {currentPage === 'recovery-queue' && (
          <RecoveryQueue />
        )}
      </main>
    </div>
  );
}

export default App;
