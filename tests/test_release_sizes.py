from pathlib import Path
import runpy
import pytest

module=runpy.run_path(str(Path(__file__).parents[1]/'scripts/check_release_sizes.py'))


@pytest.mark.parametrize('windows_full,key', [(True,'windows_full_max_exclusive'),(False,'github_asset_max_exclusive')])
@pytest.mark.parametrize('offset,accepted',[(-1,True),(0,False),(1,False)])
def test_strict_size_boundaries(windows_full,key,offset,accepted):
    limit=module['POLICY'][key]
    if accepted:
        result=module['check_size'](limit+offset,windows_full=windows_full)
        assert min(result['remaining_bytes'].values())==0
    else:
        with pytest.raises(ValueError):module['check_size'](limit+offset,windows_full=windows_full)
