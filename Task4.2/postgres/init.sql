-- Две отдельные БД в одном инстансе PostgreSQL:
--   camunda  — вторичное хранилище Camunda (Operate / Tasklist);
--   payments — данные Payment Service (платежи, журнал переходов, проводки).
CREATE USER camunda WITH PASSWORD 'camunda';
CREATE DATABASE camunda OWNER camunda;

CREATE USER payments WITH PASSWORD 'payments';
CREATE DATABASE payments OWNER payments;
