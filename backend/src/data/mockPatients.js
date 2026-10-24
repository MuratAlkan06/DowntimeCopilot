// Synthetic patient data (Synthea-like FHIR format)
export const mockPatients = [
  {
    id: "patient-001",
    name: "Sarah Chen",
    age: 68,
    snapshotTime: new Date(Date.now() - 5 * 60000).toISOString(), // 5 min ago
    allergies: [
      { code: "70618", name: "Penicillin" },
      { code: "7980", name: "Sulfonamides" }
    ],
    currentMedications: [
      {
        code: "5640",
        name: "Warfarin",
        dose: "5mg",
        frequency: "Once daily",
        lastAdministered: new Date(Date.now() - 24 * 60 * 60000).toISOString()
      },
      {
        code: "318",
        name: "Metoprolol",
        dose: "50mg",
        frequency: "Twice daily",
        lastAdministered: new Date(Date.now() - 12 * 60 * 60000).toISOString()
      },
      {
        code: "2231",
        name: "Heparin",
        dose: "5000 units",
        frequency: "Every 4 hours",
        lastAdministered: new Date(Date.now() - 2 * 60 * 60000).toISOString()
      }
    ],
    upcomingDoses: [
      { medication: "Heparin", dueAt: new Date(Date.now() + 30 * 60000).toISOString() },
      { medication: "Metoprolol", dueAt: new Date(Date.now() + 2 * 60 * 60000).toISOString() }
    ],
    recentLabs: [
      { test: "INR", value: "2.3", range: "2.0-3.0", timestamp: new Date(Date.now() - 24 * 60 * 60000).toISOString() },
      { test: "Creatinine", value: "1.2", range: "0.7-1.3", timestamp: new Date(Date.now() - 24 * 60 * 60000).toISOString() }
    ]
  },
  {
    id: "patient-002",
    name: "James Rodriguez",
    age: 45,
    snapshotTime: new Date(Date.now() - 3 * 60000).toISOString(),
    allergies: [
      { code: "314", name: "Aspirin" }
    ],
    currentMedications: [
      {
        code: "5640",
        name: "Warfarin",
        dose: "2.5mg",
        frequency: "Once daily",
        lastAdministered: new Date(Date.now() - 12 * 60 * 60000).toISOString()
      },
      {
        code: "3992",
        name: "Lisinopril",
        dose: "10mg",
        frequency: "Once daily",
        lastAdministered: new Date(Date.now() - 12 * 60 * 60000).toISOString()
      }
    ],
    upcomingDoses: [
      { medication: "Warfarin", dueAt: new Date(Date.now() + 1 * 60 * 60000).toISOString() },
      { medication: "Lisinopril", dueAt: new Date(Date.now() + 1.5 * 60 * 60000).toISOString() }
    ],
    recentLabs: [
      { test: "INR", value: "2.1", range: "2.0-3.0", timestamp: new Date(Date.now() - 24 * 60 * 60000).toISOString() },
      { test: "Potassium", value: "4.2", range: "3.5-5.0", timestamp: new Date(Date.now() - 24 * 60 * 60000).toISOString() }
    ]
  },
  {
    id: "patient-003",
    name: "Margaret Wu",
    age: 72,
    snapshotTime: new Date(Date.now() - 8 * 60000).toISOString(),
    allergies: [
      { code: "70618", name: "Penicillin" }
    ],
    currentMedications: [
      {
        code: "2101",
        name: "Acetaminophen",
        dose: "500mg",
        frequency: "Every 6 hours",
        lastAdministered: new Date(Date.now() - 3 * 60 * 60000).toISOString()
      },
      {
        code: "318",
        name: "Metoprolol",
        dose: "25mg",
        frequency: "Twice daily",
        lastAdministered: new Date(Date.now() - 12 * 60 * 60000).toISOString()
      }
    ],
    upcomingDoses: [
      { medication: "Acetaminophen", dueAt: new Date(Date.now() + 45 * 60000).toISOString() },
      { medication: "Metoprolol", dueAt: new Date(Date.now() + 2 * 60 * 60000).toISOString() }
    ],
    recentLabs: [
      { test: "Glucose", value: "145", range: "70-100", timestamp: new Date(Date.now() - 24 * 60 * 60000).toISOString() }
    ]
  }
];
