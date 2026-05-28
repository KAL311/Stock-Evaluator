from pathlib import Path
import yaml

_CONFIG_DIR = Path(__file__).resolve().parent


def _load_yaml(name):
    path = _CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(str(path), 'r') as f:
        return yaml.safe_load(f)


def load_config():
    return _load_yaml('config.yaml')


def load_sector_metrics():
    return _load_yaml('sector_metrics.yaml')


def load_regime_config():
    return _load_yaml('regime.yaml')


def load_gics_mapping():
    return _load_yaml('gics_mapping.yaml')


def get_metric_config(gics_code, sector_metrics):
    """Look up metric config for a GICS code.
    Falls back through: sub-industry -> industry -> default.
    """
    defaults = sector_metrics.get('defaults', {})
    sectors = sector_metrics.get('sectors', {})

    # Try exact match first (8-digit sub-industry or 6-digit industry)
    if gics_code in sectors:
        return sectors[gics_code]

    # Try truncating to 6-digit industry code
    if len(gics_code) >= 6:
        ind_code = gics_code[:6]
        if ind_code in sectors:
            return sectors[ind_code]

    # Return a merged default using the defaults section
    result = dict(defaults)
    result['valuation'] = defaults.get('valuation', ['fcf_yield', 'p_pe_trailing'])
    result['quality'] = defaults.get('quality', ['roic_3y_med', 'operating_margin_3y_med'])
    result['growth'] = defaults.get('growth', ['revenue_growth_3yr'])
    result['sentiment'] = defaults.get('sentiment', ['price_mom_12_1'])
    return result


def build_gics_lookup():
    """Build a mapping dict: GICS sub-industry / industry code -> metric config.
    Also builds parent relationships for fallback.
    """
    sm = load_sector_metrics()
    sectors = sm.get('sectors', {})
    return sectors


def build_simfin_to_gics():
    """Build SimFin IndustryId -> GICS code lookup."""
    gics_map = load_gics_mapping()
    mappings = gics_map.get('mappings', {})
    return {str(k): v for k, v in mappings.items()}
