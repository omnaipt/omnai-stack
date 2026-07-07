--
-- PostgreSQL database dump
--

\restrict LVzXG81V7PfvVJOmfJljx214psJjh2g7cVjamY0A9z7lCiRiZl5nj6Tt5VmRV1Y

-- Dumped from database version 16.13
-- Dumped by pg_dump version 16.13

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: briefing_items_touch(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.briefing_items_touch() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.actualizado_em := NOW();
    RETURN NEW;
END;
$$;


--
-- Name: email_inbox_touch(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.email_inbox_touch() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.actualizado_em := NOW();
    RETURN NEW;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: briefing_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.briefing_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    chave text NOT NULL,
    tipo text NOT NULL,
    urgencia text DEFAULT 'P2'::text NOT NULL,
    empresa text,
    titulo text NOT NULL,
    detalhe text,
    link_origem text,
    status text DEFAULT 'open'::text NOT NULL,
    snooze_until timestamp with time zone,
    criado_em timestamp with time zone DEFAULT now() NOT NULL,
    actualizado_em timestamp with time zone DEFAULT now() NOT NULL,
    resolvido_em timestamp with time zone,
    metadata jsonb,
    CONSTRAINT briefing_items_status_check CHECK ((status = ANY (ARRAY['open'::text, 'done'::text, 'dismissed'::text, 'snoozed'::text]))),
    CONSTRAINT briefing_items_urgencia_check CHECK ((urgencia = ANY (ARRAY['P0'::text, 'P1'::text, 'P2'::text, 'P3'::text])))
);


--
-- Name: briefing_inbox_view; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.briefing_inbox_view AS
 SELECT id,
    chave,
    tipo,
    urgencia,
    empresa,
    titulo,
    detalhe,
    link_origem,
    status,
    snooze_until,
    criado_em,
    actualizado_em,
    resolvido_em,
    metadata
   FROM public.briefing_items
  WHERE ((status = 'open'::text) OR ((status = 'snoozed'::text) AND (snooze_until <= now())))
  ORDER BY
        CASE urgencia
            WHEN 'P0'::text THEN 0
            WHEN 'P1'::text THEN 1
            WHEN 'P2'::text THEN 2
            WHEN 'P3'::text THEN 3
            ELSE NULL::integer
        END, empresa, criado_em;


--
-- Name: email_inbox; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.email_inbox (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    chave text NOT NULL,
    message_id text NOT NULL,
    thread_id text,
    account text NOT NULL,
    from_addr text,
    subject text,
    snippet text,
    classificacao text DEFAULT 'actionable'::text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    draft_text text,
    draft_generated_at timestamp with time zone,
    gmail_thread_url text,
    criado_em timestamp with time zone DEFAULT now() NOT NULL,
    actualizado_em timestamp with time zone DEFAULT now() NOT NULL,
    resolvido_em timestamp with time zone,
    CONSTRAINT email_inbox_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'done'::text, 'dismissed'::text, 'drafted'::text, 'replied'::text])))
);


--
-- Name: learned_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.learned_rules (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    rule_type text NOT NULL,
    pattern text NOT NULL,
    forced_class text NOT NULL,
    source text DEFAULT 'manual'::text NOT NULL,
    notes text,
    hit_count integer DEFAULT 0 NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    criado_em timestamp with time zone DEFAULT now() NOT NULL,
    last_hit_at timestamp with time zone,
    metadata jsonb,
    CONSTRAINT learned_rules_forced_class_check CHECK ((forced_class = ANY (ARRAY['actionable'::text, 'invoice'::text, 'archive'::text, 'delete'::text, 'keep'::text]))),
    CONSTRAINT learned_rules_rule_type_check CHECK ((rule_type = ANY (ARRAY['sender'::text, 'domain'::text, 'subject_keyword'::text]))),
    CONSTRAINT learned_rules_source_check CHECK ((source = ANY (ARRAY['manual'::text, 'sapo_trash'::text, 'auto'::text, 'seed'::text])))
);


--
-- Name: telegram_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.telegram_users (
    chat_id bigint NOT NULL,
    username text,
    first_name text,
    registered_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    urgencias text[] DEFAULT ARRAY['P0'::text] NOT NULL,
    notes text
);


--
-- Name: user_todos; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_todos (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    titulo text NOT NULL,
    detalhe text,
    prioridade text DEFAULT 'P2'::text NOT NULL,
    empresa text,
    status text DEFAULT 'open'::text NOT NULL,
    criado_em timestamp with time zone DEFAULT now() NOT NULL,
    completado_em timestamp with time zone,
    metadata jsonb,
    CONSTRAINT user_todos_prioridade_check CHECK ((prioridade = ANY (ARRAY['P0'::text, 'P1'::text, 'P2'::text, 'P3'::text]))),
    CONSTRAINT user_todos_status_check CHECK ((status = ANY (ARRAY['open'::text, 'done'::text, 'archived'::text])))
);


--
-- Name: briefing_items briefing_items_chave_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.briefing_items
    ADD CONSTRAINT briefing_items_chave_key UNIQUE (chave);


--
-- Name: briefing_items briefing_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.briefing_items
    ADD CONSTRAINT briefing_items_pkey PRIMARY KEY (id);


--
-- Name: email_inbox email_inbox_chave_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_inbox
    ADD CONSTRAINT email_inbox_chave_key UNIQUE (chave);


--
-- Name: email_inbox email_inbox_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_inbox
    ADD CONSTRAINT email_inbox_pkey PRIMARY KEY (id);


--
-- Name: learned_rules learned_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.learned_rules
    ADD CONSTRAINT learned_rules_pkey PRIMARY KEY (id);


--
-- Name: learned_rules learned_rules_rule_type_pattern_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.learned_rules
    ADD CONSTRAINT learned_rules_rule_type_pattern_key UNIQUE (rule_type, pattern);


--
-- Name: telegram_users telegram_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.telegram_users
    ADD CONSTRAINT telegram_users_pkey PRIMARY KEY (chat_id);


--
-- Name: user_todos user_todos_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_todos
    ADD CONSTRAINT user_todos_pkey PRIMARY KEY (id);


--
-- Name: idx_briefing_items_empresa; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_briefing_items_empresa ON public.briefing_items USING btree (empresa, status) WHERE (status = 'open'::text);


--
-- Name: idx_briefing_items_snooze; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_briefing_items_snooze ON public.briefing_items USING btree (snooze_until) WHERE (status = 'snoozed'::text);


--
-- Name: idx_briefing_items_status_urgencia; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_briefing_items_status_urgencia ON public.briefing_items USING btree (status, urgencia) WHERE (status = ANY (ARRAY['open'::text, 'snoozed'::text]));


--
-- Name: idx_email_inbox_account_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_email_inbox_account_status ON public.email_inbox USING btree (account, status);


--
-- Name: idx_email_inbox_status_criado; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_email_inbox_status_criado ON public.email_inbox USING btree (status, criado_em DESC);


--
-- Name: idx_learned_rules_enabled_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_learned_rules_enabled_type ON public.learned_rules USING btree (enabled, rule_type);


--
-- Name: idx_learned_rules_pattern; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_learned_rules_pattern ON public.learned_rules USING btree (pattern);


--
-- Name: idx_telegram_users_enabled; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_telegram_users_enabled ON public.telegram_users USING btree (enabled);


--
-- Name: idx_user_todos_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_user_todos_status ON public.user_todos USING btree (status, prioridade);


--
-- Name: briefing_items trg_briefing_items_touch; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_briefing_items_touch BEFORE UPDATE ON public.briefing_items FOR EACH ROW EXECUTE FUNCTION public.briefing_items_touch();


--
-- Name: email_inbox trg_email_inbox_touch; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_email_inbox_touch BEFORE UPDATE ON public.email_inbox FOR EACH ROW EXECUTE FUNCTION public.email_inbox_touch();


--
-- PostgreSQL database dump complete
--

\unrestrict LVzXG81V7PfvVJOmfJljx214psJjh2g7cVjamY0A9z7lCiRiZl5nj6Tt5VmRV1Y

