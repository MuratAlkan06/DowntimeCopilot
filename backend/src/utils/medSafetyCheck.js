// Local medication safety rules (works offline, no AI needed)

const drugInteractions = [
  { drug1: "Warfarin", drug2: "Aspirin", severity: "high", note: "Increased bleeding risk" },
  { drug1: "Warfarin", drug2: "Acetaminophen", severity: "medium", note: "May increase INR" },
  { drug1: "Heparin", drug2: "Aspirin", severity: "high", note: "Significant bleeding risk" },
  { drug1: "Metoprolol", drug2: "Acetaminophen", severity: "low", note: "No significant interaction" }
];

const allergyContraindications = [
  { allergy: "Penicillin", contraindicated: ["Amoxicillin", "Ampicillin", "Cephalexin"] },
  { allergy: "Aspirin", contraindicated: ["Aspirin"] },
  { allergy: "Sulfonamides", contraindicated: ["Sulfamethoxazole", "Sulfadiazine"] }
];

export function checkMedicationSafety(patient, proposedMedication) {
  const alerts = [];

  // Check allergies
  for (const allergy of patient.allergies) {
    if (allergyContraindications.some(ac =>
      ac.allergy === allergy.name &&
      ac.contraindicated.includes(proposedMedication)
    )) {
      alerts.push({
        type: "ALLERGY_ALERT",
        severity: "critical",
        message: `⚠️ Patient is allergic to ${allergy.name}, and you're about to give ${proposedMedication}`,
        source: `Snapshot from ${patient.snapshotTime}`
      });
    }
  }

  // Check drug interactions
  for (const med of patient.currentMedications) {
    const interaction = drugInteractions.find(di =>
      (di.drug1 === med.name && di.drug2 === proposedMedication) ||
      (di.drug1 === proposedMedication && di.drug2 === med.name)
    );

    if (interaction) {
      alerts.push({
        type: "INTERACTION_ALERT",
        severity: interaction.severity === "high" ? "critical" : "warning",
        message: `⚠️ ${interaction.note}: ${med.name} + ${proposedMedication}`,
        source: `Current medication from snapshot`
      });
    }
  }

  // Check recent administration (avoid double-dosing)
  const recentMed = patient.currentMedications.find(m => m.name === proposedMedication);
  if (recentMed && recentMed.lastAdministered) {
    const hoursSinceLastDose = (Date.now() - new Date(recentMed.lastAdministered)) / (1000 * 60 * 60);
    if (hoursSinceLastDose < 2) {
      alerts.push({
        type: "DOSE_TIMING_ALERT",
        severity: "critical",
        message: `⚠️ Last dose of ${proposedMedication} was ${hoursSinceLastDose.toFixed(1)} hours ago. Double-dose risk.`,
        source: `Last administered: ${recentMed.lastAdministered}`
      });
    }
  }

  return {
    proposedMedication,
    patientId: patient.id,
    patientName: patient.name,
    alerts,
    isSafe: alerts.length === 0,
    snapshotTime: patient.snapshotTime
  };
}

export function getUpcomingDosesList(patients) {
  const upcoming = [];
  const now = Date.now();
  const thirtyMinFromNow = now + 30 * 60 * 1000;

  for (const patient of patients) {
    for (const dose of patient.upcomingDoses) {
      const dueTime = new Date(dose.dueAt).getTime();
      if (dueTime >= now && dueTime <= thirtyMinFromNow) {
        upcoming.push({
          patientId: patient.id,
          patientName: patient.name,
          medication: dose.medication,
          dueAt: dose.dueAt,
          minutesUntilDue: Math.round((dueTime - now) / 60000)
        });
      }
    }
  }

  return upcoming.sort((a, b) => new Date(a.dueAt) - new Date(b.dueAt));
}
