// SPDX-FileCopyrightText: 2026 udisplayer contributors
// SPDX-License-Identifier: MIT
// Deterministic full-band reference tests for high-bit-depth SAO.
#include "common.h"
#include "vec/intrinsic.h"
#include <cstdio>
#include <vector>
#include <random>
#include <algorithm>
#include <cstdint>
#include <chrono>
int main() {
 std::mt19937 rng(0x96103);
 int cases=0,bad_avx=0,bad_sse=0;
 const bool avx2 = __builtin_cpu_supports("avx2");
 for (int w : {4,8,9,15,16,17,31,32,33,47,64,67})
 for (int h : {1,3,16,64})
 for (int bands=0;bands<80;bands++) {
   sao_param_t param={};
   param.startBand=rng()%32;
   param.startBand2=(bands%5==0?param.startBand: bands%7==0?(param.startBand+1)&31:rng()%32);
   for(int i=0;i<32;i++) param.offset[i]=0;
   const int b[4]={param.startBand,(param.startBand+1)&31,param.startBand2,(param.startBand2+1)&31};
   for(int i=0;i<4;i++) param.offset[b[i]]=(int)(rng()%17)-8;
   const int stride=w+20;std::vector<pel_t> src(stride*h+32),dst(stride*h+32),ref(stride*h+32),old(stride*h+32);
   for(auto &v:src)v=(pel_t)(rng()%1024);
   std::fill(dst.begin(),dst.end(),0xfade);std::fill(ref.begin(),ref.end(),0xfade);std::fill(old.begin(),old.end(),0xfade);
   for(int y=0;y<h;y++)for(int x=0;x<w;x++) {
      int v=src[y*stride+x];int o=param.offset[v>>5];ref[y*stride+x]=(pel_t)std::max(0,std::min(1023,v+o));
   }
   if (avx2) SAO_on_block_bo_avx2_10bit(dst.data(),stride,src.data(),stride,w,h,10,&param);
   SAO_on_block_bo_sse128_10bit(old.data(),stride,src.data(),stride,w,h,10,&param);
   for(int y=0;y<h;y++)for(int x=0;x<w;x++) {
     int i=y*stride+x;
     if(avx2 && dst[i]!=ref[i]){if(bad_avx++<5)printf("AVX_MISMATCH w=%d h=%d x=%d y=%d ref=%d got=%d\n",w,h,x,y,ref[i],dst[i]);}
     if(old[i]!=ref[i]){if(bad_sse++<5)printf("SSE_MISMATCH w=%d h=%d x=%d y=%d ref=%d got=%d\n",w,h,x,y,ref[i],old[i]);}
   }
   cases++;
 }
 printf("CASES=%d AVX_MISMATCHES=%d SSE_MISMATCHES=%d\n",cases,bad_avx,bad_sse);
 return (bad_avx || bad_sse) ? 1:0;
}
