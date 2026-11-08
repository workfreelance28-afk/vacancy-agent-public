"""Synthetic example evidence; contains no real candidate biography."""

from vacancy_agent.domain.application_draft import EvidenceFact, MasterProfile

MASTER_PROFILE_VERSION = "synthetic-v1"

MASTER_PROFILE = MasterProfile(
    version=MASTER_PROFILE_VERSION,
    facts=(
        EvidenceFact(
            id="role.python_junior",
            category="role",
            statement="Целевая роль учебного кандидата — Junior Python-разработчик.",
            source="Учебное резюме (полностью вымышленное)",
        ),
        EvidenceFact(
            id="skill.python",
            category="skill",
            statement="Учебный кандидат использует Python и pytest.",
            source="Учебное резюме (полностью вымышленное)",
        ),
        EvidenceFact(
            id="skill.sql",
            category="skill",
            statement="Учебный кандидат использует SQL и SQLite.",
            source="Учебное резюме (полностью вымышленное)",
        ),
        EvidenceFact(
            id="project.csv_example",
            category="project",
            statement=(
                "Учебный кандидат создал локальный обработчик CSV "
                "с проверкой входных данных."
            ),
            source="Учебное резюме (полностью вымышленное)",
        ),
        EvidenceFact(
            id="experience.learning_boundary",
            category="experience",
            statement=(
                "Три месяца учебной практики не означают "
                "коммерческий опыт или senior-уровень."
            ),
            source="Учебное резюме (полностью вымышленное)",
        ),
    ),
)
