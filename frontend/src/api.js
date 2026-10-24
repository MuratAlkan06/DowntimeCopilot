import axios from 'axios';

const API_BASE = '/api';

const api = axios.create({
  baseURL: API_BASE,
  headers: {
    'Content-Type': 'application/json'
  }
});

export const patients = {
  list: () => api.get('/patients'),
  get: (id) => api.get(`/patients/${id}`)
};

export const medSafety = {
  check: (patientId, medication) =>
    api.post('/med-safety-check', { patientId, medication })
};

export const doses = {
  upcoming: () => api.get('/upcoming-doses')
};

export const downtimeNotes = {
  create: (patientId, noteType, content, source = 'manual') =>
    api.post('/downtime-notes', { patientId, noteType, content, source }),
  list: (patientId = null, status = null) => {
    const params = {};
    if (patientId) params.patientId = patientId;
    if (status) params.status = status;
    return api.get('/downtime-notes', { params });
  },
  update: (id, reviewStatus) =>
    api.patch(`/downtime-notes/${id}`, { reviewStatus })
};

export default api;
