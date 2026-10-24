import { useState, useEffect } from 'react';
import { downtimeNotes, patients } from '../api';
import './RecoveryQueue.css';

function RecoveryQueue() {
  const [notes, setNotes] = useState([]);
  const [patientMap, setPatientMap] = useState({});
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState('needs_review');

  useEffect(() => {
    Promise.all([
      downtimeNotes.list(),
      patients.list()
    ])
      .then(([notesRes, patientsRes]) => {
        setNotes(notesRes.data.data);
        const map = {};
        patientsRes.data.data.forEach(p => {
          map[p.id] = p.name;
        });
        setPatientMap(map);
        setLoading(false);
      })
      .catch(err => {
        console.error('Error loading data:', err);
        setLoading(false);
      });
  }, []);

  const handleApprove = async (noteId) => {
    try {
      const result = await downtimeNotes.update(noteId, 'approved');
      setNotes(notes.map(n => n.id === noteId ? result.data : n));
    } catch (err) {
      console.error('Error approving note:', err);
    }
  };

  const handleDiscard = async (noteId) => {
    try {
      const result = await downtimeNotes.update(noteId, 'discarded');
      setNotes(notes.map(n => n.id === noteId ? result.data : n));
    } catch (err) {
      console.error('Error discarding note:', err);
    }
  };

  if (loading) return <div className="loading">Loading recovery queue...</div>;

  const filteredNotes = notes.filter(n => n.reviewStatus === filter);
  const stats = {
    total: notes.length,
    needsReview: notes.filter(n => n.reviewStatus === 'needs_review').length,
    approved: notes.filter(n => n.reviewStatus === 'approved').length,
    discarded: notes.filter(n => n.reviewStatus === 'discarded').length
  };

  return (
    <div className="recovery-queue">
      <h2>📋 Recovery Queue</h2>

      <div className="queue-stats">
        <div className="stat">
          <span className="label">To Review</span>
          <span className="number">{stats.needsReview}</span>
        </div>
        <div className="stat">
          <span className="label">Approved</span>
          <span className="number">{stats.approved}</span>
        </div>
        <div className="stat">
          <span className="label">Total</span>
          <span className="number">{stats.total}</span>
        </div>
      </div>

      <div className="filter-tabs">
        <button
          className={filter === 'needs_review' ? 'active' : ''}
          onClick={() => setFilter('needs_review')}
        >
          Needs Review ({stats.needsReview})
        </button>
        <button
          className={filter === 'approved' ? 'active' : ''}
          onClick={() => setFilter('approved')}
        >
          Approved ({stats.approved})
        </button>
      </div>

      {filteredNotes.length === 0 ? (
        <div className="empty-state">
          <p>No notes in this category</p>
        </div>
      ) : (
        <div className="notes-queue">
          {filteredNotes.map(note => (
            <div key={note.id} className={`queue-item status-${note.reviewStatus}`}>
              <div className="item-header">
                <div className="item-title">
                  <span className="patient-name">{patientMap[note.patientId] || 'Unknown'}</span>
                  <span className="note-type">{note.noteType}</span>
                </div>
                <div className="item-time">
                  {new Date(note.timestamp).toLocaleTimeString()}
                </div>
              </div>

              <div className="item-content">
                <p>{note.content}</p>
              </div>

              <div className="item-meta">
                <span className="source">Source: {note.source}</span>
                <span className="status">{note.reviewStatus}</span>
              </div>

              {note.reviewStatus === 'needs_review' && (
                <div className="item-actions">
                  <button className="btn-approve" onClick={() => handleApprove(note.id)}>
                    ✅ Approve & Queue for Re-entry
                  </button>
                  <button className="btn-discard" onClick={() => handleDiscard(note.id)}>
                    ❌ Discard
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      <div className="queue-note">
        <p>
          💡 <strong>When the EHR returns:</strong> approved notes are ready to re-enter, sorted by clinical priority (meds first).
          Staff verifies each entry to catch any transcription errors before saving to the EHR.
        </p>
      </div>
    </div>
  );
}

export default RecoveryQueue;
