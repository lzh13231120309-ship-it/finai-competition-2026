"""Validate installed dependencies and models without uploading documents."""
from pathlib import Path
import sys, os, hashlib, importlib
ROOT=Path(__file__).resolve().parents[1]; STAGE=ROOT/'第一阶段_金融提取'
sys.path[:0]=[str(ROOT/'金融大赛_集成试用版'),str(STAGE)]
def main():
    assert sys.version_info[:2]==(3,13), '本版本以 Python 3.13 验证'
    for name in ['fastapi','uvicorn','httpx','docx','pptx','openpyxl','mineru','mineru_llama_cpp','pytest']:importlib.import_module(name)
    os.environ.update(MINERU_HOME=str(ROOT/'runtime/mineru'), MINERU_CONFIG=str(STAGE/'mineru-local.yaml'), MINERU_MODEL_BASE_DIR=str(ROOT/'runtime/models'),MINERU_MODEL_SMALL_BACKEND='onnx',MINERU_MODEL_VLM_ENGINE='llama-cpp')
    from mineru.model.registry import model_repos_for_tier
    from mineru.model.download import verify_model_repo
    for repo in model_repos_for_tier('standard',small_backend='onnx',vlm_engine='llama-cpp'):
        result=verify_model_repo(repo)
        if not result.ready:raise RuntimeError(f'{repo.name}缺少模型文件：{result.missing_paths}')
        print(f'{repo.name}检查通过')
    model=ROOT/'runtime/models/Qwen3-4B-GGUF/Qwen3-4B-Q4_K_M.gguf'
    with model.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
    if digest!='7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5':raise RuntimeError('Qwen模型指纹不符')
    import local_reasoning
    if not local_reasoning.executable_path().is_file():raise RuntimeError(f'未找到本地推理引擎：{local_reasoning.executable_path()}')
    print('依赖、MinerU资源与Qwen指纹检查通过；这不等于真实Agent推理与OCR已经验收。')
if __name__=='__main__':main()
