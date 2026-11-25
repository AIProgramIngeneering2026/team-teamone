-- 002_seed_spaces.sql
-- Начальный каталог помещений кампуса для прототипа API.

INSERT OR IGNORE INTO spaces (space_id, category, name) VALUES
    ('CW-10', 'coworking', 'Коворкинг №10'),
    ('CW-11', 'coworking', 'Коворкинг №11'),
    ('PZ-01', 'project_zone', 'Проектная зона №1'),
    ('ROOM-204', 'classroom', 'Семинарская аудитория №204'),
    ('ROOM-301', 'classroom', 'Аудитория №301'),
    ('LAB-01', 'lab', 'Учебная лаборатория №1'),
    ('HALL-700', 'conference_hall', 'Конференц-зал на 700 мест');
