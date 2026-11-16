// SPDX-FileCopyrightText: 2026 udisplayer contributors
// SPDX-License-Identifier: MIT
// Horizontal EO boundaries, arbitrary block dimensions and exact 10-bit pixels.
#include "common.h"
#include "vec/intrinsic.h"
#include <vector>
#include <random>
#include <algorithm>
#include <cstdio>
int main() {
 std::mt19937 rng(907331);int cases=0,bad_avx=0,bad_sse=0;
 const bool avx2 = __builtin_cpu_supports("avx2");
 for(int width:{4,5,7,8,9,15,16,17,31,32,33,47,64,67})
 for(int height:{1,3,17,64})
 for(int left: {0,1})for(int right:{0,1})
 for(int test=0;test<30;test++){
  const int stride=width+32, pad=8;std::vector<pel_t> src(stride*height+64),a(stride*height+64,0xface),b(stride*height+64,0xface),ref(stride*height+64,0xface);
  for(auto &x:src)x=(pel_t)(rng()%1024);
  int avail[8]={0};avail[2]=left;avail[3]=right;
  int off[5];for(int i=0;i<5;i++)off[i]=(int)(rng()%19)-9;
  for(int y=0;y<height;y++)for(int x=(left?0:1);x<(right?width:width-1);x++){
   const int center=src[pad+y*stride+x];
   const int l=src[pad+y*stride+x-1],r=src[pad+y*stride+x+1];
   const int category=2+(center>l)-(center<l)+(center>r)-(center<r);
   ref[pad+y*stride+x]=(pel_t)std::max(0,std::min(1023,center+off[category]));
  }
  if (avx2) SAO_on_block_eo_0_avx2_10bit(a.data()+pad,stride,src.data()+pad,stride,width,height,10,avail,off);
  SAO_on_block_eo_0_sse128_10bit(b.data()+pad,stride,src.data()+pad,stride,width,height,10,avail,off);
  for(int y=0;y<height;y++)for(int x=0;x<width;x++){
   int pos=pad+y*stride+x;
   if(avx2 && a[pos]!=ref[pos]){if(bad_avx++<5)printf("AVXDIFF w=%d h=%d left=%d right=%d x=%d y=%d got=%d ref=%d\n",width,height,left,right,x,y,a[pos],ref[pos]);}
   if(b[pos]!=ref[pos]){if(bad_sse++<5)printf("SSEDIFF w=%d h=%d left=%d right=%d x=%d y=%d got=%d ref=%d\n",width,height,left,right,x,y,b[pos],ref[pos]);}
  }
  cases++;
 }
 printf("EO0_CASES=%d AVX_DIFF=%d SSE_DIFF=%d\n",cases,bad_avx,bad_sse);
 return (bad_avx || bad_sse)?1:0;
}
