import base64
import io
import re
import json
import zipfile
from collections import defaultdict

try:
    from pypdf import PdfReader as _PdfReader
except ImportError:
    _PdfReader = None

import pandas as pd
import networkx as nx
import yaml

import dash
from dash import dcc, html, dash_table
from dash.dependencies import Input, Output, State
import plotly.graph_objects as go

# ==============================================================================
# CARREGAMENTO DE DADOS
# ==============================================================================
try:
    df = pd.read_excel("catalogo_disciplinas_graduacao_2024_2025.xlsx")
    df["SIGLA_BASE"] = df["SIGLA"].apply(lambda x: re.sub(r"-\d{2}$", "", str(x)))
    with open("siglas_cursos.yaml", "r", encoding="utf-8") as file:
        cursos_yaml = yaml.safe_load(file)
except FileNotFoundError as e:
    print(f"Erro: Arquivo não encontrado - {e}")
    exit()

df["CURSO"] = df["SIGLA"].str.extract(r"(^[A-Z]+)")[0]
df["RECOMENDACAO_LIMPA"] = (
    df["RECOMENDAÇÃO"].fillna("").str.replace(r"[\n\r]", " ", regex=True)
)

# Mapa nome_disciplina (maiúsculo) -> sigla  (para resolver recomendações)
NOME_PARA_SIGLA: dict[str, str] = {
    row["DISCIPLINA"].upper(): row["SIGLA_BASE"] for _, row in df.iterrows()
}
THEMES = json.load(open("themes.json", "r", encoding="utf-8"))
_CSS = open("layout.css", "r", encoding="utf-8").read()


def obter_nome_curso(sigla: str) -> str:
    curso_prefixo = sigla[:2]
    eixo_sigla = sigla[2]
    if curso_prefixo in cursos_yaml["cursos"]["BI"]:
        nome_curso = cursos_yaml["cursos"]["BI"][curso_prefixo]
        if eixo_sigla in cursos_yaml["cursos"]["BI"]["eixo"]:
            nome_eixo = cursos_yaml["cursos"]["BI"]["eixo"][eixo_sigla]
            return f"BI - {nome_curso} - Eixo: {nome_eixo}"
        return f"BI - {nome_curso}"
    elif curso_prefixo in cursos_yaml["cursos"]["POS BI"]:
        nome_centro = cursos_yaml["cursos"]["POS BI"][curso_prefixo]
        curso_detalhado = sigla[2:4]
        if curso_detalhado in cursos_yaml["cursos_detalhados"]:
            nome_det = cursos_yaml["cursos_detalhados"][curso_detalhado]
            return f"POS BI - {nome_centro} - {nome_det}"
        return f"POS BI - {nome_centro} - Curso não encontrado"
    return "Curso não encontrado"


df["NOME_CURSO"] = df["CURSO"].apply(obter_nome_curso)

_DEFAULT_THEME = "light"
# ==============================================================================
# PARSING DO HISTÓRICO (ZIP com pages .txt)
# ==============================================================================
_STATUS_PAT = re.compile(
    r"\b(APR|APRN|DISP|CUMP|REP|REPF|REPMF|REPN|REPNF|TRANS|INCORP)\b"
)
_COMPONENTES_PAT = re.compile(
    r"\b(OBR|OL|LIV|ATC)\b"
)
_PERIOD_PAT = re.compile(r"\b(\d{4}\.[123])\b")
_FULL_SIGLA_PAT = re.compile(r"([A-Z]{3,6}\d{2,7}-\d{2})")
_SPLIT_SIGLA_PAT = re.compile(r"([A-Z]{3,6}\d{2,7})\s+(\d-\d{2}|\d{1,2})\b")
_CONCEITO_PAT = re.compile(r"\s([A|B|C|D|F|O])\s")
_SIGLA_FLEX_PAT = re.compile(r"([A-Z]{3,6}\d{2,7})")


# Conceitos em ordem do melhor para o pior (excluindo situações especiais)
CONCEITO_PESO: dict[str, int] = {
    "A": 4,
    "B": 3,
    "C": 2,
    "D": 1,
    "F": 0,
    "O": -1,   # abandonou (conceito mais baixo)
    "--": None,
}

STATUS_APROVACAO = {"APR", "APRN", "DISP", "CUMP", "TRANS", "INCORP"}


def _normalizar_sigla_historico(sigla_raw: str) -> str:
    """Remove o sufixo de turma (-NN) deixando só o código base."""
    return sigla_raw  # mantemos completo para cruzar com catálogo


def _extrair_texto_sigaa_zip(decoded: bytes) -> list[str] | None:
    """
    Tenta ler o histórico no formato interno do SIGAA: um arquivo .pdf que é
    na verdade um ZIP contendo páginas .txt e manifest.json.
    Retorna lista de textos por página, ou None se não for esse formato.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(decoded)) as zf:
            if "manifest.json" not in zf.namelist():
                return None
            manifest = json.loads(zf.read("manifest.json"))
            return [
                zf.read(p["text"]["path"]).decode("utf-8")
                for p in sorted(manifest["pages"], key=lambda x: x["page_number"])
            ]
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError):
        return None


def _extrair_texto_pdf_real(decoded: bytes) -> list[str] | None:
    """
    Tenta ler um PDF real usando pypdf.
    Retorna lista de textos por página, ou None se falhar.
    """
    if _PdfReader is None:
        return None
    try:
        reader = _PdfReader(io.BytesIO(decoded))
        return [page.extract_text(extraction_mode="layout") or "" for page in reader.pages]
    except Exception:
        return None

def parse_historico_pdf(contents: str) -> tuple[dict | None, str]:
    """
    Lê o histórico acadêmico exportado pelo SIGAA/UFABC.

    Aceita dois formatos:
    - Arquivo .pdf exportado pelo SIGAA (internamente é um ZIP com páginas .txt)
    - PDF real (fallback via pypdf)

    Retorna (course_history, mensagem_status).

    course_history = {
        "BCN0402-15": [
            {"periodo": "2022.1", "situacao": "REPF", "conceito": "O"},
            {"periodo": "2023.1", "situacao": "APR",  "conceito": "C"},
        ],
        ...
    }
    """
    try:
        _content_type, content_string = contents.split(",", 1)
        decoded = base64.b64decode(content_string)
    except (ValueError, Exception) as e:
        return None, f"Formato de conteúdo inválido: {e}"

    # Tenta o formato nativo do SIGAA (ZIP disfarçado de PDF)
    pages_text = _extrair_texto_sigaa_zip(decoded)

    # Fallback: PDF real
    if pages_text is None:
        pages_text = _extrair_texto_pdf_real(decoded)

    if pages_text is None:
        return (
            None,
            "Não foi possível ler o arquivo. Certifique-se de enviar o "
            "histórico oficial exportado pelo SIGAA (Menu: Ensino → "
            "Histórico → Emitir Histórico Escolar).",
        )

    full_text = "\n".join(pages_text).replace("\r\n", "\n")
    lines = [l.strip() for l in full_text.split("\n")]

    legend_idx = next(
        (i for i, l in enumerate(lines) if l == "Legenda"), len(lines)
    )
    lines = lines[:legend_idx]

    # ------------------------------------------------------------------
    # PRÉ-PROCESSAMENTO: reconstrói linhas fragmentadas pelo layout PDF.
    #
    # O SIGAA quebra cada disciplina em (pelo menos) duas linhas:
    #
    #   Linha A:  "<SIGLA_BASE>  <nome>  <carga>  ...  <STATUS>"
    #   Linha B:  "<sufixo -NN>  <YYYY.Q>  <notas>  <conceito>"
    #
    # Ou, para reprovadas, o sufixo pode vir após o quadrimestre:
    #   Linha B:  "<YYYY.Q>  <sufixo -NN>  <notas>  <conceito>"
    #
    # Estratégia: quando uma linha contém STATUS mas a sigla extraída
    # não tem sufixo (-NN), tentamos completá-la com a linha seguinte.
    # ------------------------------------------------------------------

    _SUFIXO_PAT = re.compile(r"\w\s\s\s\s\s\s\s(\d{2})|(\d-\d+)|-(\d+)")
    # Padrão para detectar quadrimestre sozinho no início da linha
    _QUAD_INICIO_PAT = re.compile(r"^\d{4}\.[1-3]\b")
    course_history: dict[str, list] = defaultdict(list)
    current_period: str | None = None
    # merged_lines: list[str] = []
    skip_next = False
    for i, line in enumerate(lines):
        if skip_next:
            skip_next = False
            continue

        sm = _STATUS_PAT.search(line)
        if sm:
            status = sm.group(1)
            # Verifica se a sigla na linha atual já tem sufixo completo
            sigla_m = _FULL_SIGLA_PAT.search(line)
            if sigla_m:
                
                # Sigla completa (com -NN): linha já OK
                sigla: str | None = sigla_m.group(1) if sigla_m else None
                # print(f"Encontrada sigla completa: {sigla} na linha: {i}")
            else:
                # Sigla incompleta: tenta completar com a próxima linha
                next_line = lines[i - 1] if i - 1 < len(lines) else ""
                # next_stripped = next_line.strip()
                combined = line + " " + next_line
                sigla_m = _SIGLA_FLEX_PAT.search(combined)
                sigla_base: str | None = sigla_m.group(1) if sigla_m else None
                suf_m = _SUFIXO_PAT.search(combined)
                sufixo = None
                quad_m = _QUAD_INICIO_PAT.search(combined)

                if suf_m:
                    sufixo = next((g for g in suf_m.groups() if g is not None), None)
                    # print(f"Encontrada sigla incompleta: {sigla_base} na linha: {i} com sufixo: {sufixo}")
                    if "-" not in sufixo:
                        sigla = f"{sigla_base}-{sufixo}"
                    else:
                        sigla = f"{sigla_base}{sufixo}"
                    if sufixo is None: 
                        print(f"Não foi possível extrair sufixo para {sigla_base} na linha {i}, {combined}.Grupos do sufixo: {suf_m.groups() if suf_m else 'None'}")
                    skip_next = True
                
                if not sigla:
                    continue
            periodo_m = _PERIOD_PAT.search(line) 
            periodo = periodo_m.group(1) if periodo_m else (current_period or "Período desconhecido")            
            conceito_m = _CONCEITO_PAT.search(line)
            conceito = conceito_m.group(1) if conceito_m else "--"

            course_history[sigla].append(
                {"periodo": periodo, "situacao": status, "conceito": conceito}
            )

    if not course_history:
        return None, "Nenhuma disciplina encontrada. Verifique se o arquivo é um histórico SIGAA/UFABC válido."

    return dict(course_history), f"Histórico processado com sucesso! {len(course_history)} disciplinas encontradas."

# ==============================================================================
# HELPERS DE HISTÓRICO
# ==============================================================================

def historico_para_sigla_base(course_history: dict) -> dict:
    """
    Cria um mapa de sigla_base (sem versão de turma) → lista de tentativas.
    Ex: 'BCN0402' → [...] (agrupa BCN0402-15, BCN0402-17, etc.)
    Também mantém chave exata.
    """
    resultado = {}
    for sigla_completa, tentativas in course_history.items():
        resultado[sigla_completa] = tentativas
        # Também indexa pelo código sem sufixo para matching flexível
        base = re.sub(r"-\d{2}$", "", sigla_completa)
        if base not in resultado:
            resultado[base] = tentativas
    return resultado


def resumo_disciplina(sigla: str, course_history: dict) -> dict | None:
    """
    Retorna um resumo das tentativas de uma disciplina pelo aluno.
    Busca por sigla exata e por prefixo (ignora sufixo -NN).
    """
    hist_expandido = historico_para_sigla_base(course_history)
    base = re.sub(r"-\d{2}$", "", sigla)

    tentativas = hist_expandido.get(sigla) or hist_expandido.get(base)
    if not tentativas:
        return None

    ultima = tentativas[-1]
    aprovacoes = sum(1 for t in tentativas if t["situacao"] in STATUS_APROVACAO)
    reprovacoes = len(tentativas) - aprovacoes

    return {
        "tentativas": len(tentativas),
        "aprovacoes": aprovacoes,
        "reprovacoes": reprovacoes,
        "ultimo_periodo": ultima["periodo"],
        "ultima_situacao": ultima["situacao"],
        "ultimo_conceito": ultima["conceito"],
        "aprovado": aprovacoes > 0,
        "historico": tentativas,
    }


def siglas_aprovadas(course_history: dict) -> set[str]:
    """Retorna conjunto de siglas base das disciplinas aprovadas."""
    aprovadas = set()
    for sigla, tentativas in course_history.items():
        if any(t["situacao"] in STATUS_APROVACAO for t in tentativas):
            base = re.sub(r"-\d{2}$", "", sigla)
            aprovadas.add(sigla)
            aprovadas.add(base)
    return aprovadas


# ==============================================================================
# GRAFO
# ==============================================================================

def criar_grafo_completo(df_catalogo: pd.DataFrame):
    """Cria dígrafo com todas as disciplinas e suas recomendações."""
    G = nx.DiGraph()
    codigo_para_nome = dict(zip(df_catalogo["SIGLA"], df_catalogo["DISCIPLINA"]))
 
    for codigo, nome in codigo_para_nome.items():
        G.add_node(codigo, label=nome)
 
    for _, row in df_catalogo.iterrows():
        origem = row["SIGLA"]
        for item in row["RECOMENDACAO_LIMPA"].split(";"):
            item_limpo = item.strip()
            if not item_limpo or item_limpo.lower() in ("não há", "nao ha", ""):
                continue
            # Busca por nome exato (case-insensitive)
            item_upper = item_limpo.upper()
            for cod, nome in codigo_para_nome.items():
                if item_upper in nome.upper() and cod != origem:
                    G.add_edge(cod, origem)
    return G, codigo_para_nome
 
 
def gerar_subgrafo(G: nx.DiGraph, sigla: str, course_history: dict | None,
                   theme: dict | None = None):
    """Gera figura Plotly do subgrafo centrado em `sigla`, respeitando o tema."""
    t = theme or THEMES[_DEFAULT_THEME]
 
    vizinhos = set(G.predecessors(sigla)) | set(G.successors(sigla)) | {sigla}
    SG = G.subgraph(vizinhos)
    pos = nx.spring_layout(SG, seed=42)
 
    aprovadas = siglas_aprovadas(course_history) if course_history else set()
 
    edge_x_rec, edge_y_rec = [], []
    edge_x_suc, edge_y_suc = [], []
 
    for edge in SG.edges():
        x0, y0 = pos[edge[0]]
        x1, y1 = pos[edge[1]]
        if edge[1] == sigla:
            edge_x_rec += [x0, x1, None]
            edge_y_rec += [y0, y1, None]
        else:
            edge_x_suc += [x0, x1, None]
            edge_y_suc += [y0, y1, None]
 
    trace_rec = go.Scatter(
        x=edge_x_rec, y=edge_y_rec, mode="lines",
        line=dict(width=1.5, color=t["danger"]), hoverinfo="none", name="Pré-requisito"
    )
    trace_suc = go.Scatter(
        x=edge_x_suc, y=edge_y_suc, mode="lines",
        line=dict(width=1.5, color=t["muted"]), hoverinfo="none", name="Recomenda"
    )
 
    node_x, node_y, node_text, node_colors, node_symbols, node_sizes = [], [], [], [], [], []
    hover_texts = []
 
    for node in SG.nodes():
        x, y = pos[node]
        node_x.append(x)
        node_y.append(y)
 
        label = SG.nodes[node].get("label", node)
        node_text.append(f"{node}")
 
        is_prereq = sigla in G.successors(node)
        is_center = node == sigla
        resumo = resumo_disciplina(node, course_history) if course_history else None
 
        if is_center:
            color = t["accent"]
            symbol = "circle"
            size = 24
        elif is_prereq:
            color = t["danger"]
            symbol = "circle"
            size = 18
        else:
            color = t["info"]
            symbol = "circle"
            size = 18
 
        if resumo:
            if resumo["aprovado"]:
                color = t["accent"] if is_center else t["warning"]
                symbol = "circle"
                size += 4
            else:
                symbol = "diamond"
 
        node_colors.append(color)
        node_symbols.append(symbol)
        node_sizes.append(size)
 
        hover = f"<b>{node}</b><br>{label}"
        if resumo:
            conceito_display = resumo['ultimo_conceito']
            hover += (
                f"<br>─────────────"
                f"<br>📋 Tentativas: {resumo['tentativas']}"
                f"<br>✅ Aprovações: {resumo['aprovacoes']}"
                f"<br>❌ Reprovações: {resumo['reprovacoes']}"
                f"<br>📅 Último período: {resumo['ultimo_periodo']}"
                f"<br>📊 Último conceito: <b>{conceito_display}</b>"
                f"<br>🏷️ Situação: {resumo['ultima_situacao']}"
            )
        else:
            hover += "<br>─────────────<br><i>Não cursada</i>"
        hover_texts.append(hover)
 
    node_trace = go.Scatter(
        x=node_x, y=node_y,
        mode="markers+text",
        text=node_text,
        hovertext=hover_texts,
        hoverinfo="text",
        textposition="top center",
        marker=dict(
            size=node_sizes,
            color=node_colors,
            symbol=node_symbols,
            line=dict(width=2, color=t["surface"]),
        ),
    )
 
    center_label = SG.nodes[sigla].get("label", sigla) if sigla in SG.nodes else sigla
    fig = go.Figure(
        data=[trace_rec, trace_suc, node_trace],
        layout=go.Layout(
            title=dict(
                text=f"<b>{sigla}</b> — {center_label}",
                font=dict(color=t["text"], size=14, family="'Inter', sans-serif"),
            ),
            showlegend=False,
            margin=dict(b=20, l=5, r=5, t=50),
            xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            plot_bgcolor=t["plot_bg"],
            paper_bgcolor=t["plot_paper"],
            font=dict(color=t["text"], family="'Inter', sans-serif"),
            hovermode="closest",
        ),
    )
    return fig


# ==============================================================================
# ENGINE DE SUGESTÃO DE MATÉRIAS
# ==============================================================================

def calcular_sugestoes(course_history: dict, df_catalogo: pd.DataFrame, top_n: int = 20) -> list[dict]:
    """
    Pontua cada disciplina ainda não aprovada e retorna as mais recomendadas.

    Critérios:
    - recomendacoes_cumpridas: fração das disciplinas recomendadas já aprovadas
    - penalidade_reprovacoes: penaliza cada reprovação prévia na disciplina
    - bonus_sem_historico: disciplinas sem nenhuma tentativa partem de zero (neutro)

    Score = recomendacoes_cumpridas * 10  (0–10)
            - penalidade_reprovacoes       (0 a -∞, mas limitado)
    Quanto maior, mais sugerida.
    """
    aprovadas = siglas_aprovadas(course_history)
    hist_expandido = historico_para_sigla_base(course_history)

    # Mapa nome -> sigla para resolver recomendações
    nome_upper_para_sigla = {
        row["DISCIPLINA"].upper(): row["SIGLA"]
        for _, row in df_catalogo.iterrows()
    }

    resultados = []

    for _, row in df_catalogo.iterrows():
        sigla = row["SIGLA"]
        base = re.sub(r"-\d{2}$", "", sigla)

        # Pula disciplinas já aprovadas
        if sigla in aprovadas or base in aprovadas:
            continue

        # --- Recomendações cumpridas ---
        recomendacoes_raw = row["RECOMENDACAO_LIMPA"].strip()
        recomendacoes_siglas: list[str] = []

        if recomendacoes_raw and recomendacoes_raw.lower() not in ("não há", "nao ha"):
            for item in recomendacoes_raw.split(";"):
                item_u = item.strip().upper()
                sigla_rec = nome_upper_para_sigla.get(item_u)
                if sigla_rec:
                    recomendacoes_siglas.append(sigla_rec)

        total_recs = len(recomendacoes_siglas)
        recs_cumpridas = 0
        penalidade_recs_reprovadas = 0.0
        if total_recs > 0:
            for sigla_rec in recomendacoes_siglas:
                base_rec = re.sub(r"-\d{2}$", "", sigla_rec)
                if sigla_rec in aprovadas or base_rec in aprovadas:
                    recs_cumpridas += 1
                    tentativas_rec = hist_expandido.get(sigla_rec) or hist_expandido.get(base_rec) or []
                    reprovacoes_rec = sum(1 for t in tentativas_rec if t["situacao"] not in STATUS_APROVACAO)
                    # Subtrai 1.0 ponto para cada reprovação em cada matéria recomendada
                    penalidade_recs_reprovadas += (reprovacoes_rec * 1.0)
            frac_recs = recs_cumpridas / total_recs
        else:
            # Sem recomendações formais: disciplina livre.
            # Pontuamos como 0.5 (neutro) para não inflar o ranking acima
            # de disciplinas com recomendações reais já cumpridas.
            frac_recs = 0.5
            recs_cumpridas = 0

        # --- Histórico do aluno nesta disciplina ---
        tentativas_prev = hist_expandido.get(sigla) or hist_expandido.get(base) or []
        reprovacoes_prev = sum(
            1 for t in tentativas_prev if t["situacao"] not in STATUS_APROVACAO
        )

        # Penalidade por reprovações: cada reprovação subtrai 1.5 pontos (máx -6)
        penalidade = min(reprovacoes_prev * 1.5, 6.0)

        # Score final
        score = frac_recs * 10 - penalidade - penalidade_recs_reprovadas

        resultados.append({
            "SIGLA": sigla,
            "DISCIPLINA": row["DISCIPLINA"],
            "SCORE": round(score, 2),
            "RECS_CUMPRIDAS": f"{recs_cumpridas}/{total_recs}" if total_recs > 0 else "Sem recomendações",
            "FRAC_RECS_%": f"{frac_recs * 100:.0f}%",
            "PENALIDADE_RECS": round(penalidade_recs_reprovadas, 1),
            "TENTATIVAS_PREV": len(tentativas_prev),
            "REPROVACOES_PREV": reprovacoes_prev,
        })

    resultados.sort(key=lambda x: x["SCORE"], reverse=True)
    return resultados[:top_n]


# ==============================================================================
# APLICAÇÃO DASH
# ==============================================================================

def _t(theme_data: dict | None, key: str) -> str:
    """Retorna uma cor do tema, com fallback para dark."""
    t = theme_data or THEMES[_DEFAULT_THEME]
    return t.get(key, THEMES[_DEFAULT_THEME][key])
 
 
def _table_styles(theme_data: dict | None):
    """Retorna estilos de DataTable adequados ao tema."""
    t = theme_data or THEMES[_DEFAULT_THEME]
    return dict(
        style_table={"overflowX": "auto", "borderRadius": "8px", "border": f"1px solid {t['table_border']}"},
        style_cell={
            "fontSize": "0.87rem", "padding": "8px 12px",
            "backgroundColor": t["surface"], "color": t["table_text"],
            "border": f"1px solid {t['table_border']}",
            "fontFamily": "'Inter', sans-serif",
        },
        style_header={
            "fontWeight": "700", "backgroundColor": t["table_header"],
            "color": t["table_text"], "border": f"1px solid {t['table_border']}",
            "fontSize": "0.8rem", "textTransform": "uppercase", "letterSpacing": "0.5px",
        },
        style_data_conditional_base=[
            {"if": {"row_index": "odd"}, "backgroundColor": t["table_even"]},
        ],
    )

app = dash.Dash(__name__, suppress_callback_exceptions=True)
 
app.index_string = """<!DOCTYPE html>
<html>
<head>
  {%metas%}
  <title>Análise Acadêmica UFABC</title>
  {%favicon%}
  {%css%}
  <style>""" + _CSS + """</style>
</head>
<body>
  {%app_entry%}
  <footer>{%config%}{%scripts%}{%renderer%}</footer>
</body>
</html>"""


app.layout = html.Div([
    dcc.Location(id="url", refresh=False),
    dcc.Store(id="historico-data-store", storage_type="session"),
    dcc.Store(id="theme-store", data=_DEFAULT_THEME, storage_type="local"),
    html.Div(id="_theme-init-dummy", style={"display": "none"}),
 
    # ── Navbar ──────────────────────────────────────────────────────────────
    html.Nav(className="ufabc-nav", children=[
        html.H1("🌿 Análise Acadêmica UFABC"),
        html.Div(className="nav-right", children=[
            dcc.Link("🔗 Grafo",      href="/",          className="nav-link"),
            dcc.Link("📋 Histórico",  href="/historico", className="nav-link"),
            dcc.Link("💡 Sugestões",  href="/sugestoes", className="nav-link"),
            html.Button("🌙 Dark", id="theme-toggle-btn", n_clicks=0),
        ]),
    ]),
 
    html.Div(id="page-content", className="page-content"),
])

# ==============================================================================
# Globais
# ==============================================================================
app.clientside_callback(
    """
    function(theme) {
        const t = theme || 'dark';
        document.documentElement.setAttribute('data-theme', t);
        return window.dash_clientside.no_update;
    }
    """,
    Output("_theme-init-dummy", "children"),
    Input("theme-store", "data"),
)
 
# Toggle dark / light ao clicar no botão
app.clientside_callback(
    """
    function(n, current) {
        const next = (current || 'dark') === 'dark' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', next);
        return next;
    }
    """,
    Output("theme-store", "data"),
    Input("theme-toggle-btn", "n_clicks"),
    State("theme-store", "data"),
    prevent_initial_call=True,
)
@app.callback(
    Output("theme-toggle-btn", "children"),
    Input("theme-store", "data"),
)
def update_btn_label(theme):
    return "☀️ Light" if (theme or _DEFAULT_THEME) == "dark" else "🌙 Dark"
 
# ==============================================================================
# PÁGINA: GRAFO  (/)
# ==============================================================================
 
def create_layout_grafo():
    return html.Div([
        html.H2("Grafo de Pré-requisitos e Recomendações"),
 
        html.Div([
            html.Div([
                html.Label("Selecione uma disciplina:"),
                dcc.Dropdown(
                    id="disciplina-dropdown",
                    options=[
                        {"label": f"{row['DISCIPLINA']} ({row['SIGLA']})", "value": row["SIGLA"]}
                        for _, row in df.iterrows()
                    ],
                    value=df["SIGLA"].iloc[0] if not df.empty else None,
                    style={"fontSize": "0.88rem", "marginTop": "4px"},
                ),
            ], style={"flex": "1", "marginRight": "20px"}),
 
            html.Div([
                html.Label("Histórico carregado:"),
                html.Div(id="grafo-historico-status",
                         style={"fontStyle": "italic", "marginTop": "4px", "fontSize": "0.88rem"}),
            ], style={"minWidth": "240px"}),
        ], style={"display": "flex", "alignItems": "flex-end", "marginBottom": "12px"}),
 
        # Painel de info da disciplina selecionada
        html.Div(id="grafo-info-panel", style={"marginBottom": "12px"}),
 
        dcc.Graph(id="grafo-disciplina", clickData=None, style={"height": "530px", "borderRadius": "10px"}),
 
        # Legenda
        html.Div(className="legend-panel", children=[
            html.H4("Legenda"),
            html.Div([
                _badge("🟢", "#00C853",  "Disciplina selecionada"),
                _badge("🟡", "#FFD740",  "Já cursada e aprovada"),
                _badge("🔴", "#FF5252",  "Pré-requisito recomendado"),
                _badge("🔵", "#40C4FF",  "Disciplina que a selecionada recomenda"),
                html.Span("◆ Diamante = tentou mas não aprovou",
                          style={"marginLeft": "16px", "fontSize": "0.85rem", "opacity": "0.7"}),
            ], style={"display": "flex", "flexWrap": "wrap", "gap": "14px", "alignItems": "center"}),
        ]),
    ])
 
 
def _badge(emoji, color, label):
    return html.Span([
        html.Span(emoji, style={"marginRight": "4px"}),
        html.Span(label, style={"color": color, "fontWeight": "600", "fontSize": "0.88rem"}),
    ], style={"marginRight": "8px"})
 
 
@app.callback(
    Output("grafo-historico-status", "children"),
    Input("historico-data-store", "data"),
)
def atualizar_status_historico(data):
    if not data:
        return "Nenhum histórico — vá em Histórico para importar."
    return f"✅ {len(data)} disciplinas carregadas."
 
 
@app.callback(
    Output("grafo-info-panel", "children"),
    Input("disciplina-dropdown", "value"),
    State("historico-data-store", "data"),
    State("theme-store", "data"),
)
def atualizar_painel_info(sigla, course_history, theme):
    if not sigla:
        return html.Div()
 
    t = THEMES.get(theme or _DEFAULT_THEME, THEMES[_DEFAULT_THEME])
    row = df[df["SIGLA"] == sigla]
    nome = row["DISCIPLINA"].iloc[0] if not row.empty else sigla
 
    children = [html.Strong(f"{sigla} — {nome}", style={"fontSize": "0.95rem"})]
 
    if course_history:
        resumo = resumo_disciplina(sigla, course_history)
        if resumo:
            conceito = resumo["ultimo_conceito"]
            cor_conceito = {"A": "#00C853", "B": "#69F0AE", "C": "#FFD740",
                            "D": "#FF9800", "F": "#FF5252", "O": "#CE93D8"}.get(conceito, t["muted"])
            status_icon = "✅" if resumo["aprovado"] else "❌"
            sep = html.Span("  ·  ", style={"color": t["muted"], "margin": "0 4px"})
            children += [
                html.Span(f"  {status_icon}", style={"marginLeft": "10px"}),
                sep,
                html.Span(f"Tentativas: ", style={"color": t["muted"], "fontSize": "0.88rem"}),
                html.Strong(resumo['tentativas'], style={"fontSize": "0.88rem"}),
                sep,
                html.Span(f"Aprovações: ", style={"color": t["muted"], "fontSize": "0.88rem"}),
                html.Strong(resumo['aprovacoes'], style={"color": "#00C853", "fontSize": "0.88rem"}),
                sep,
                html.Span(f"Reprovações: ", style={"color": t["muted"], "fontSize": "0.88rem"}),
                html.Strong(resumo['reprovacoes'], style={"color": t["danger"], "fontSize": "0.88rem"}),
                sep,
                html.Span(f"Período: ", style={"color": t["muted"], "fontSize": "0.88rem"}),
                html.Strong(resumo['ultimo_periodo'], style={"fontSize": "0.88rem"}),
                sep,
                html.Span("Conceito: ", style={"color": t["muted"], "fontSize": "0.88rem"}),
                html.Strong(conceito, style={"color": cor_conceito, "fontSize": "1.05rem"}),
                html.Span(f"  ({resumo['ultima_situacao']})", style={"color": t["muted"], "fontSize": "0.82rem"}),
            ]
        else:
            children.append(html.Span("  —  Não cursada",
                                       style={"color": t["muted"], "marginLeft": "10px", "fontSize": "0.88rem"}))
 
    return html.Div(children, className="panel", style={"fontSize": "0.95rem"})
 
 
@app.callback(
    Output("grafo-disciplina", "figure"),
    Input("disciplina-dropdown", "value"),
    Input("theme-store", "data"),
    State("historico-data-store", "data"),
)
def atualizar_grafo(sigla, theme, course_history):
    if not sigla:
        return go.Figure()
    t = THEMES.get(theme or _DEFAULT_THEME, THEMES[_DEFAULT_THEME])
    G, _ = criar_grafo_completo(df)
    if sigla in G:
        return gerar_subgrafo(G, sigla, course_history or {}, t)
    return go.Figure()
 
 
@app.callback(
    Output("disciplina-dropdown", "value"),
    Input("grafo-disciplina", "clickData"),
    State("disciplina-dropdown", "value"),
)
def atualizar_dropdown_por_clique(clickData, current_value):
    if clickData and "points" in clickData and clickData["points"]:
        node_id = clickData["points"][0].get("text", "").split(":")[0].strip()
        if node_id in df["SIGLA"].values:
            return node_id
    return current_value
 
 
# ==============================================================================
# PÁGINA: HISTÓRICO  (/historico)
# ==============================================================================
 
def create_layout_historico():
    return html.Div([
        html.H2("Importar Histórico Acadêmico"),
 
        html.P(
            "Faça o upload do arquivo exportado pelo SIGAA (.pdf). "
            "Acesse: Ensino → Histórico → Emitir Histórico Escolar.",
        ),
 
        dcc.Upload(
            id="upload-data",
            children=html.Div([
                html.Span("📄  ", style={"fontSize": "1.4rem"}),
                html.Br(),
                "Arraste e solte ou ",
                html.A("selecione seu Histórico (.pdf)", style={"color": "#00C853", "fontWeight": "600"}),
            ], style={"lineHeight": "1.4"}),
            accept=".pdf",
            className="upload-zone",
            style={
                "width": "100%", "minHeight": "90px",
                "display": "flex", "alignItems": "center", "justifyContent": "center",
                "textAlign": "center", "margin": "14px 0", "padding": "20px",
            },
        ),
 
        html.Div(id="output-data-upload"),
    ])
 
 
@app.callback(
    Output("output-data-upload", "children"),
    Output("historico-data-store", "data"),
    Input("upload-data", "contents"),
    State("upload-data", "filename"),
    State("theme-store", "data"),
)
def processar_historico(contents, filename, theme):
    t = THEMES.get(theme or _DEFAULT_THEME, THEMES[_DEFAULT_THEME])
 
    if contents is None:
        return html.Div("Por favor, carregue seu histórico para iniciar a análise.",
                        style={"color": t["muted"], "fontStyle": "italic"}), {}
 
    course_history, status_message = parse_historico_pdf(contents)
 
    if not course_history:
        return html.Div([
            html.H5("❌ Erro ao processar o arquivo", style={"color": t["danger"]}),
            html.P(status_message),
        ]), {}
 
    APROVACAO = STATUS_APROVACAO
    ts = _table_styles(t)
 

    summary_data = []
    for sigla, tentativas in course_history.items():
        aprovacoes = sum(1 for t_ in tentativas if t_["situacao"] in APROVACAO)
        reprovacoes = len(tentativas) - aprovacoes
        ultima = tentativas[-1]
        disc_row = df[df["SIGLA"] == sigla]
        nome_disc = disc_row["DISCIPLINA"].iloc[0] if not disc_row.empty else "—"
        conceito_val = ultima["conceito"]
        summary_data.append({
            "SIGLA":        sigla,
            "DISCIPLINA":   nome_disc,
            "TENTATIVAS":   len(tentativas),
            "APROVACOES":   aprovacoes,
            "REPROVACOES":  reprovacoes,
            "ULT_PERIODO":  ultima["periodo"],
            "ULT_CONCEITO": conceito_val,
            "ULT_SITUACAO": ultima["situacao"],
        })
 
    summary_data.sort(key=lambda x: (x["REPROVACOES"], -x["TENTATIVAS"]), reverse=True)
 
    # ── Log detalhado de tentativas ─────────────────────────────────────────
    log_data = [
        {"PERIODO": t_["periodo"], "SIGLA": sigla, "SITUACAO": t_["situacao"], "CONCEITO": t_["conceito"]}
        for sigla, tentativas in course_history.items()
        for t_ in tentativas
    ]
    log_data.sort(key=lambda x: (x["PERIODO"] or "0000.0", x["SIGLA"]))
 
    # ── Estatísticas gerais ─────────────────────────────────────────────────
    total_disc = len(course_history)
    total_aprovadas = sum(
        1 for t_list in course_history.values()
        if any(t_["situacao"] in APROVACAO for t_ in t_list)
    )
    total_rep = sum(
        1 for t_list in course_history.values()
        if any(t_["situacao"] not in APROVACAO for t_ in t_list)
    )
 
    cond_summary = ts["style_data_conditional_base"] + [
        {"if": {"filter_query": '{REPROVACOES} > 0', "column_id": "REPROVACOES"},
         "color": t["danger"], "fontWeight": "bold"},
        {"if": {"filter_query": '{ULT_CONCEITO} = "A"', "column_id": "ULT_CONCEITO"},
         "color": t["accent"], "fontWeight": "bold"},
        {"if": {"filter_query": '{ULT_CONCEITO} = "O"', "column_id": "ULT_CONCEITO"},
         "color": "#CE93D8", "fontWeight": "bold"},
        {"if": {"filter_query": '{ULT_CONCEITO} = "F"', "column_id": "ULT_CONCEITO"},
         "color": t["danger"]},
        {"if": {"filter_query": '{APROVACOES} > 0', "column_id": "ULT_SITUACAO"},
         "backgroundColor": t["success_bg"]},
    ]
 
    cond_log = ts["style_data_conditional_base"] + [
        {"if": {"filter_query": '{SITUACAO} = "APR"'}, "backgroundColor": t["success_bg"]},
        {"if": {"filter_query": '{SITUACAO} = "REPF"'}, "backgroundColor": t["danger_bg"]},
        {"if": {"filter_query": '{SITUACAO} = "REP"'},  "backgroundColor": t["danger_bg"]},
        {"if": {"filter_query": '{CONCEITO} = "O"', "column_id": "CONCEITO"},
         "color": "#CE93D8", "fontWeight": "bold"},
    ]
 
    return html.Div([
        html.Div([
            html.Span("✅ ", style={"fontSize": "1.1rem"}),
            html.Strong(filename),
            html.Span(f"  —  {status_message}", style={"color": t["muted"], "fontSize": "0.9rem"}),
        ], className="panel", style={"marginBottom": "20px", "fontSize": "0.95rem",
                                      "borderLeft": f"4px solid {t['accent']}"}),
 
        # Cards de estatísticas
        html.Div(className="stat-row", children=[
            _stat_card("Total de disciplinas", total_disc,   t["info"],    t),
            _stat_card("Aprovadas",            total_aprovadas, t["accent"], t),
            _stat_card("Com reprovação",        total_rep,    t["danger"],  t),
        ]),
 
        html.H3("Resumo por Disciplina"),
        html.P("Ordenado por mais reprovações → mais tentativas.",
               style={"fontSize": "0.84rem", "marginBottom": "10px"}),
        dash_table.DataTable(
            data=summary_data,
            columns=[
                {"name": "Sigla",           "id": "SIGLA"},
                {"name": "Disciplina",      "id": "DISCIPLINA"},
                {"name": "Tentativas",      "id": "TENTATIVAS"},
                {"name": "Aprovações",      "id": "APROVACOES"},
                {"name": "Reprovações",     "id": "REPROVACOES"},
                {"name": "Último Período",  "id": "ULT_PERIODO"},
                {"name": "Último Conceito", "id": "ULT_CONCEITO"},
                {"name": "Última Situação", "id": "ULT_SITUACAO"},
            ],
            style_data_conditional=cond_summary,
            style_table=ts["style_table"],
            style_cell=ts["style_cell"],
            style_header=ts["style_header"],
            page_size=15,
            sort_action="native",
            filter_action="native",
        ),
 
        html.H3("Log Detalhado de Tentativas", style={"marginTop": "30px"}),
        dash_table.DataTable(
            data=log_data,
            columns=[
                {"name": "Período",  "id": "PERIODO"},
                {"name": "Sigla",    "id": "SIGLA"},
                {"name": "Situação", "id": "SITUACAO"},
                {"name": "Conceito", "id": "CONCEITO"},
            ],
            style_data_conditional=cond_log,
            style_table=ts["style_table"],
            style_cell=ts["style_cell"],
            style_header=ts["style_header"],
            page_size=20,
            sort_action="native",
            filter_action="native",
        ),
    ]), course_history
 
 
def _stat_card(label, value, color, t):
    return html.Div([
        html.Div(str(value), style={"fontSize": "2.2rem", "fontWeight": "700", "color": color,
                                     "lineHeight": "1", "marginBottom": "4px"}),
        html.Div(label, style={"fontSize": "0.8rem", "color": t["muted"],
                                "textTransform": "uppercase", "letterSpacing": "0.5px", "fontWeight": "600"}),
    ], style={
        "background": t["surface"], "border": f"2px solid {color}",
        "borderRadius": "10px", "padding": "18px 24px", "minWidth": "155px",
        "textAlign": "center", "boxShadow": "0 2px 10px rgba(0,0,0,0.15)",
    })

# ==============================================================================
# PÁGINA: SUGESTÕES  (/sugestoes)
# ==============================================================================
def create_layout_sugestoes():
    return html.Div([
        html.H2("💡 Sugestões de Matérias"),
        html.P(
            "Ranqueamento das disciplinas ainda não aprovadas, considerando: "
            "proporção de recomendações já cumpridas, histórico de reprovações "
            "e disponibilidade de pré-requisitos.",
        ),
        html.Div(id="sugestoes-content"),
    ])
 
 
@app.callback(
    Output("sugestoes-content", "children"),
    Input("historico-data-store", "data"),
    State("theme-store", "data"),
)
def atualizar_sugestoes(course_history, theme):
    t = THEMES.get(theme or _DEFAULT_THEME, THEMES[_DEFAULT_THEME])
 
    if not course_history:
        return html.Div([
            html.Div([
                html.Span("⚠️  ", style={"fontSize": "1.2rem"}),
                html.Strong("Nenhum histórico carregado."),
                html.Span(" Importe na aba "),
                dcc.Link("Histórico", href="/historico", style={"color": t["accent"]}),
                html.Span(" para ver as sugestões."),
            ], className="panel", style={"borderLeft": f"4px solid {t['warning']}"}),
        ])
 
    sugestoes = calcular_sugestoes(course_history, df, top_n=30)
 
    if not sugestoes:
        return html.Div("🎉 Parabéns! Nenhuma disciplina pendente encontrada no catálogo.",
                        className="panel",
                        style={"color": t["accent"], "fontWeight": "600",
                               "borderLeft": f"4px solid {t['accent']}"})
 
    ts = _table_styles(t)
 
    cond = ts["style_data_conditional_base"] + [
        {"if": {"filter_query": "{SCORE} >= 8", "column_id": "SCORE"},
         "backgroundColor": t["success_bg"], "color": t["accent"], "fontWeight": "bold"},
        {"if": {"filter_query": "{SCORE} >= 5 && {SCORE} < 8", "column_id": "SCORE"},
         "backgroundColor": t["info_bg"], "color": t["warning"]},
        {"if": {"filter_query": "{SCORE} < 5", "column_id": "SCORE"},
         "backgroundColor": t["danger_bg"], "color": t["danger"]},
        {"if": {"filter_query": "{REPROVACOES_PREV} > 0", "column_id": "REPROVACOES_PREV"},
         "color": t["danger"], "fontWeight": "bold"},
        {"if": {"filter_query": "{REPROVACOES_RECS} > 0", "column_id": "REPROVACOES_RECS"},
         "color": t["danger"], "fontWeight": "bold"},
    ]
 
    return html.Div([
        html.H4(f"Top {len(sugestoes)} disciplinas recomendadas",
                style={"marginBottom": "10px"}),
 
        html.Div(className="panel", style={"marginBottom": "16px", "fontSize": "0.85rem"}, children=[
            html.Div([
                html.Strong("Score = "),
                "recomendações cumpridas × 10  −  reprovações prévias × 1.5 - reprovações em recomendações",
            ], style={"marginBottom": "10px", "color": t["muted"]}),
            html.Div(style={"display": "flex", "gap": "16px", "flexWrap": "wrap"}, children=[
                _score_badge("≥ 8",  t["accent"],  "#000" if theme == "light" else "#000",
                             "Alta prioridade — pré-requisitos completos"),
                _score_badge("5–8",  t["warning"], "#000", "Média prioridade"),
                _score_badge("< 5",  t["danger"],  "#fff", "Baixa — histórico de reprovações"),
            ]),
        ]),
 
        dash_table.DataTable(
            data=sugestoes,
            columns=[
                {"name": "Sigla",                "id": "SIGLA"},
                {"name": "Disciplina",            "id": "DISCIPLINA"},
                {"name": "Score",                 "id": "SCORE"},
                {"name": "Recs. Cumpridas",       "id": "RECS_CUMPRIDAS"},
                {"name": "% Recs.",               "id": "FRAC_RECS_%"},
                {"name": "Tentativas Anteriores", "id": "TENTATIVAS_PREV"},
                {"name": "Reprovações Anteriores","id": "REPROVACOES_PREV"},
                {"name": "Reprovações em Recomendações", "id": "REPROVACOES_RECS"},
            ],
            style_data_conditional=cond,
            style_table=ts["style_table"],
            style_cell=ts["style_cell"],
            style_header=ts["style_header"],
            page_size=15,
            sort_action="native",
            filter_action="native",
        ),
    ])
 
 
def _score_badge(label, bg, fg, tooltip):
    return html.Span([
        html.Span(label, style={
            "backgroundColor": bg, "color": fg,
            "padding": "3px 10px", "borderRadius": "4px",
            "fontWeight": "700", "marginRight": "6px", "fontSize": "0.82rem",
        }),
        html.Span(tooltip, style={"fontSize": "0.82rem", "opacity": "0.75"}),
    ])


# ==============================================================================
# ROTEADOR PRINCIPAL
# ==============================================================================


@app.callback(
    Output("page-content", "children"),
    Input("url", "pathname"),
)
def display_page(pathname):
    if pathname == "/historico":
        return create_layout_historico()
    elif pathname == "/sugestoes":
        return create_layout_sugestoes()
    else:
        return create_layout_grafo()


# ==============================================================================
# EXECUÇÃO
# ==============================================================================
if __name__ == "__main__":
    app.run(debug=True)