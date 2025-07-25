#!/usr/bin/env python3
"""
논문 임베딩 생성 및 Supabase 업로드 스크립트
OpenAI text-embedding-3-small 모델을 사용하여 논문 제목과 초록을 임베딩합니다.
"""

import os
import pandas as pd
import numpy as np
from typing import List, Dict, Tuple
import asyncio
import aiohttp
import json
from supabase import create_client, Client
from dotenv import load_dotenv
import time
from tqdm import tqdm
import logging

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

class EmbeddingGenerator:
    def __init__(self):
        # OpenAI API 설정
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.openai_base_url = "https://api.openai.com/v1"
        self.model_name = "text-embedding-3-small"
        
        # Supabase 설정
        self.supabase_url = os.getenv("SUPABASE_URL")
        self.supabase_key = os.getenv("SUPABASE_KEY")
        self.supabase: Client = create_client(self.supabase_url, self.supabase_key)
        
        # 배치 설정
        self.batch_size = 100  # OpenAI API 배치 크기
        self.max_retries = 3
        self.retry_delay = 1
        
        if not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY 환경변수가 설정되지 않았습니다.")
        
        if not self.supabase_url or not self.supabase_key:
            raise ValueError("Supabase 환경변수가 설정되지 않았습니다.")
    
    async def get_embedding(self, session: aiohttp.ClientSession, text: str) -> List[float]:
        """OpenAI API를 사용하여 텍스트 임베딩을 생성합니다."""
        headers = {
            "Authorization": f"Bearer {self.openai_api_key}",
            "Content-Type": "application/json"
        }
        
        data = {
            "input": text,
            "model": self.model_name
        }
        
        for attempt in range(self.max_retries):
            try:
                async with session.post(
                    f"{self.openai_base_url}/embeddings",
                    headers=headers,
                    json=data
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        return result["data"][0]["embedding"]
                    else:
                        error_text = await response.text()
                        logger.error(f"OpenAI API 오류: {response.status} - {error_text}")
                        
                        if response.status == 429:  # Rate limit
                            wait_time = (attempt + 1) * self.retry_delay * 2
                            logger.info(f"Rate limit 도달. {wait_time}초 대기...")
                            await asyncio.sleep(wait_time)
                        else:
                            raise Exception(f"API 오류: {response.status}")
                            
            except Exception as e:
                logger.error(f"임베딩 생성 실패 (시도 {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay)
                else:
                    raise
        
        raise Exception("최대 재시도 횟수 초과")
    
    async def get_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        """배치로 여러 텍스트의 임베딩을 생성합니다."""
        async with aiohttp.ClientSession() as session:
            tasks = []
            for text in texts:
                task = self.get_embedding(session, text)
                tasks.append(task)
            
            embeddings = await asyncio.gather(*tasks, return_exceptions=True)
            
            # 오류 처리
            valid_embeddings = []
            for i, emb in enumerate(embeddings):
                if isinstance(emb, Exception):
                    logger.error(f"텍스트 {i} 임베딩 실패: {emb}")
                    valid_embeddings.append(None)
                else:
                    valid_embeddings.append(emb)
            
            return valid_embeddings
    
    def prepare_text_for_embedding(self, title: str, abstract: str) -> str:
        """제목과 초록을 결합하여 임베딩용 텍스트를 준비합니다."""
        # 텍스트 정리
        title = title.strip() if title else ""
        abstract = abstract.strip() if abstract else ""
        
        # 결합 (제목을 더 중요하게 가중치)
        combined_text = f"Title: {title}\n\nAbstract: {abstract}"
        return combined_text
    
    def update_paper_embeddings(self, paper_id: int, title_embedding: List[float], 
                               abstract_embedding: List[float], 
                               combined_embedding: List[float]) -> bool:
        """Supabase에 논문 임베딩을 업데이트합니다."""
        try:
            # vector 타입으로 변환
            title_emb_str = f"[{','.join(map(str, title_embedding))}]"
            abstract_emb_str = f"[{','.join(map(str, abstract_embedding))}]"
            combined_emb_str = f"[{','.join(map(str, combined_embedding))}]"
            
            # Supabase 함수 호출
            result = self.supabase.rpc(
                'update_paper_embeddings',
                {
                    'paper_id': paper_id,
                    'title_emb': title_emb_str,
                    'abstract_emb': abstract_emb_str,
                    'combined_emb': combined_emb_str
                }
            ).execute()
            
            return True
            
        except Exception as e:
            logger.error(f"논문 {paper_id} 임베딩 업데이트 실패: {e}")
            return False
    
    async def process_papers_batch(self, papers_batch: pd.DataFrame) -> int:
        """배치 단위로 논문 임베딩을 처리합니다."""
        successful_updates = 0
        
        # 텍스트 준비
        texts_for_embedding = []
        for _, paper in papers_batch.iterrows():
            combined_text = self.prepare_text_for_embedding(paper['title'], paper['abstract'])
            texts_for_embedding.append(combined_text)
        
        # 임베딩 생성
        logger.info(f"배치 임베딩 생성 중... ({len(texts_for_embedding)}개)")
        embeddings = await self.get_embeddings_batch(texts_for_embedding)
        
        # 개별 임베딩 생성 (제목, 초록)
        title_texts = papers_batch['title'].tolist()
        abstract_texts = papers_batch['abstract'].tolist()
        
        title_embeddings = await self.get_embeddings_batch(title_texts)
        abstract_embeddings = await self.get_embeddings_batch(abstract_texts)
        
        # Supabase 업데이트
        for i, (_, paper) in enumerate(papers_batch.iterrows()):
            if (embeddings[i] is not None and 
                title_embeddings[i] is not None and 
                abstract_embeddings[i] is not None):
                
                success = self.update_paper_embeddings(
                    paper['id'],
                    title_embeddings[i],
                    abstract_embeddings[i],
                    embeddings[i]
                )
                
                if success:
                    successful_updates += 1
        
        return successful_updates
    
    async def generate_embeddings_for_all_papers(self, csv_path: str = None, 
                                                limit: int = None) -> Dict:
        """모든 논문에 대해 임베딩을 생성합니다."""
        logger.info("임베딩 생성 프로세스 시작")
        
        # CSV 파일 읽기
        if csv_path:
            df = pd.read_csv(csv_path)
            logger.info(f"CSV 파일 로드: {len(df)}개 논문")
        else:
            # Supabase에서 논문 데이터 가져오기
            result = self.supabase.table("papers").select("*").execute()
            df = pd.DataFrame(result.data)
            logger.info(f"Supabase에서 로드: {len(df)}개 논문")
        
        # 임베딩이 없는 논문만 필터링
        df_without_embeddings = df[df['combined_embedding'].isna() | (df['combined_embedding'] == '')]
        
        if limit:
            df_without_embeddings = df_without_embeddings.head(limit)
        
        logger.info(f"임베딩 생성 대상: {len(df_without_embeddings)}개 논문")
        
        if len(df_without_embeddings) == 0:
            logger.info("모든 논문에 임베딩이 이미 생성되어 있습니다.")
            return {"total_processed": 0, "successful_updates": 0}
        
        # 배치 처리
        total_processed = 0
        successful_updates = 0
        
        for i in tqdm(range(0, len(df_without_embeddings), self.batch_size), 
                     desc="임베딩 생성 진행률"):
            batch = df_without_embeddings.iloc[i:i+self.batch_size]
            
            try:
                batch_success = await self.process_papers_batch(batch)
                successful_updates += batch_success
                total_processed += len(batch)
                
                logger.info(f"배치 완료: {len(batch)}개 중 {batch_success}개 성공")
                
                # Rate limit 방지
                await asyncio.sleep(0.1)
                
            except Exception as e:
                logger.error(f"배치 처리 실패: {e}")
                continue
        
        # 통계 출력
        stats = {
            "total_processed": total_processed,
            "successful_updates": successful_updates,
            "success_rate": (successful_updates / total_processed * 100) if total_processed > 0 else 0
        }
        
        logger.info(f"임베딩 생성 완료: {stats}")
        return stats
    
    def get_embedding_stats(self) -> Dict:
        """임베딩 통계를 조회합니다."""
        try:
            result = self.supabase.rpc('get_embedding_stats').execute()
            return result.data[0] if result.data else {}
        except Exception as e:
            logger.error(f"통계 조회 실패: {e}")
            return {}

async def main():
    """메인 실행 함수"""
    print("논문 임베딩 생성기")
    print("=" * 50)
    
    try:
        # 임베딩 생성기 초기화
        generator = EmbeddingGenerator()
        
        # 현재 통계 확인
        print("현재 임베딩 통계:")
        stats = generator.get_embedding_stats()
        for key, value in stats.items():
            print(f"  {key}: {value}")
        print()
        
        # 사용자 입력
        choice = input("실행할 작업을 선택하세요:\n1. 전체 논문 임베딩 생성\n2. 제한된 수의 논문만 처리\n3. 통계만 확인\n선택: ")
        
        if choice == "1":
            limit = None
        elif choice == "2":
            limit = int(input("처리할 논문 수를 입력하세요: "))
        elif choice == "3":
            return
        else:
            print("잘못된 선택입니다.")
            return
        
        # 임베딩 생성 실행
        start_time = time.time()
        results = await generator.generate_embeddings_for_all_papers(limit=limit)
        end_time = time.time()
        
        print(f"\n처리 완료!")
        print(f"총 처리 시간: {end_time - start_time:.2f}초")
        print(f"처리된 논문: {results['total_processed']}개")
        print(f"성공한 업데이트: {results['successful_updates']}개")
        print(f"성공률: {results['success_rate']:.1f}%")
        
        # 최종 통계 확인
        print("\n최종 임베딩 통계:")
        final_stats = generator.get_embedding_stats()
        for key, value in final_stats.items():
            print(f"  {key}: {value}")
        
    except Exception as e:
        logger.error(f"실행 중 오류 발생: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(main()) 