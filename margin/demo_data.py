"""A handful of realistic-looking papers so you can see the whole pipeline work offline."""
from datetime import date, timedelta


def _d(n):
    return (date.today() - timedelta(days=n)).isoformat()


PAPERS = [
    dict(source="pubmed", source_id="90000001", doi="10.1000/demo.001", title="Prime editing corrects a sickle cell mutation with fewer off-target edits",
         abstract="We compare CRISPR-based prime editors in human haematopoietic stem cells and report a sharp drop in unintended edits across 14 loci.",
         authors=["Okafor C", "Lindqvist H", "Reyes M", "Tran P"], venue="Nature Biotechnology", published=_d(2), url="https://example.org/1", is_oa=True),
    dict(source="biorxiv", source_id="demo.002", doi="10.1101/demo.002", title="A compact base editor for in vivo CRISPR delivery by a single AAV vector",
         abstract="Shrinking the deaminase domain allows base editing in mouse liver from one viral vector, with durable editing after 24 weeks.",
         authors=["Tanaka K", "Brandt L", "Osei A"], venue="bioRxiv", published=_d(3), url="https://example.org/2", is_oa=True, is_preprint=True),
    dict(source="arxiv", source_id="2610.00003", doi="10.48550/arxiv.2610.00003", title="Scaling laws for large language models on low-resource languages",
         abstract="We fit scaling curves for large language models across 40 languages and show that data quality, not quantity, drives most of the gap.",
         authors=["Haddad R", "Novak J", "Iyer S", "Wu T"], venue="arXiv", published=_d(1), url="https://arxiv.org/abs/2610.00003", is_oa=True, is_preprint=True),
    dict(source="openalex", source_id="W900004", doi="10.1000/demo.004", title="Do large language models reason? A review of benchmark validity",
         abstract="This review examines whether popular benchmarks for large language models measure reasoning or memorised patterns.",
         authors=["Moreau E", "Chen L"], venue="Computational Linguistics", published=_d(4), url="https://example.org/4"),
    dict(source="pubmed", source_id="90000005", doi="10.1000/demo.005", title="Gut microbiome composition predicts response to immunotherapy in melanoma",
         abstract="In a 312-patient cohort, microbiome profiling before treatment separated responders from non-responders with 81% accuracy.",
         authors=["Vasquez D", "Aaltonen P", "Dube N"], venue="Cell Reports Medicine", published=_d(0), url="https://example.org/5", is_oa=True),
    dict(source="pubmed", source_id="90000006", doi="10.1000/demo.006", title="CRISPR screens reveal regulators of T cell exhaustion",
         abstract="A genome-wide CRISPR screen in primary human T cells identifies 11 regulators whose loss restores effector function.",
         authors=["Nakamura Y", "Ellis R", "Bianchi G"], venue="Science Immunology", published=_d(3), url="https://example.org/6"),
    dict(source="pubmed", source_id="90000007", doi="10.1000/demo.007", title="A systematic review of CRISPR clinical trials registered since 2019",
         abstract="A review of 87 registered trials describing indications, delivery methods and reported safety outcomes.",
         authors=["Bennett A", "Zhao Q"], venue="The Lancet Haematology", published=_d(4), url="https://example.org/7"),
    dict(source="arxiv", source_id="2610.00008", doi="10.48550/arxiv.2610.00008", title="Quantum error correction thresholds for surface codes under correlated noise",
         abstract="We derive thresholds for surface codes when noise is correlated across neighbouring qubits.",
         authors=["Fischer M", "Dlamini Z"], venue="arXiv", published=_d(2), url="https://arxiv.org/abs/2610.00008", is_oa=True, is_preprint=True),
]
