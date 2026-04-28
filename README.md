# 🎓 Planejador Acadêmico UFABC

### Olá, sofredor entusiasta! 
Planejar o quadrimestre na UFABC não precisa ser uma prova de resistência. Este projeto foi criado para transformar o seu histórico escolar do SIGAA em um **Grafo Interativo de Disciplinas**, ajudando você a visualizar sua jornada e tomar decisões baseadas em dados.

---

## 🚀 O que este projeto faz?

O core da aplicação está no arquivo `grafoc_disciplinas.py`, que gerencia três pilares principais:

*   **📊 Visualização em Grafos**: Cria automaticamente diagramas de pré-requisitos e recomendações a partir do catálogo oficial da UFABC (XLS).
*   **📑 Análise Inteligente de Histórico**: Processa o PDF exportado do SIGAA, lidando com as inconsistências de layout e fragmentação de siglas (como códigos divididos em duas linhas ou siglas com sufixos variáveis).
*   **💡 Algoritmo de Sugestão**: Ranqueia as matérias pendentes utilizando um sistema de **Score Acadêmico**.
    *   **Bônus**: Pontua mais alto disciplinas com recomendações já cumpridas.
    *   **Penalidade**: Considera reprovações prévias tanto na própria disciplina quanto nas suas recomendadas para ajustar o nível de prioridade.

---

## 🛠️ Como funciona a "Mágica"?

A aplicação utiliza um fluxo de dados integrado entre três páginas principais:

1.  **Página do Grafo**: Explore as conexões entre matérias. Ao clicar em um nó, você vê detalhes de tentativas, conceitos obtidos e se já cumpriu as recomendações.
2.  **Página de Histórico**: Onde você faz o upload do seu histórico do SIGAA. O sistema converte o PDF em dados estruturados, tratando siglas complexas como `BCN0402-15` ou `BCN0402 6-15` de forma automática.
3.  **Página de Sugestões**: Uma lista priorizada do que cursar a seguir, calculando o peso de cada reprovação no seu caminho acadêmico.

---

## 🎯 Motivação

O intuito desta solução é **otimizar o tempo do aluno** na escolha de matérias. Em vez de abrir dezenas de abas e manuais, o aluno tem uma visão clara de quais disciplinas ele tem mais base para cursar no momento, respeitando seu histórico individual e as conexões do projeto pedagógico da UFABC.

---

## 📦 Tecnologias Utilizadas

*   **Python / Dash**: Para a interface web interativa.
*   **NetworkX**: Para o processamento da estrutura de grafos.
*   **pypdf / pdfplumber**: Para a extração robusta de dados do histórico.
*   **Pandas**: Para manipulação do catálogo oficial.
*   **PyYAML**: Para gerenciamento das configurações de cursos.

---

## ⚙️ Requisitos para rodar

Para rodar o projeto localmente, certifique-se de ter os seguintes arquivos na raiz do projeto:
*   `catalogo_disciplinas_graduacao_2024_2025.xlsx`
*   `siglas_cursos.yaml`
```bash
# Exemplo de execução
python grafoc_disciplinas.py
