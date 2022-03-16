import math
from typing import Optional

import torch
import bmtrain as bmt
from .linear import Linear


class Attention(bmt.DistributedModule):
    def __init__(self, dim_in : int, 
                       dim_head : int,
                       num_heads : int, 
                       dim_out = None,
                       dtype = torch.half,
                       int8 = False, 
                       init_mean = 0.0, 
                       init_std = 0.02,
                       bias = False,
                       mask_value = float("-inf"),
                       pos_bias_type = "none",
                       length_scale : bool = False,
                       attn_scale : bool = False,
                       dropout_p = 0,
                       ):

        super().__init__()

        if dim_out is None:
            dim_out = dim_in

        self.project_q = Linear(
            dim_in = dim_in,
            dim_out = num_heads * dim_head,
            length_scale = length_scale,
            length_scale_before = False,
            dtype = dtype,
            int8 = int8,
            init_mean = init_mean,
            init_std = init_std,
            bias = bias,
        )

        self.project_k = Linear(
            dim_in = dim_in,
            dim_out = num_heads * dim_head,
            length_scale = length_scale,
            length_scale_before = False,
            dtype = dtype,
            int8 = int8,
            init_mean = init_mean,
            init_std = init_std,
            bias = bias,
        )

        self.project_v = Linear(
            dim_in = dim_in,
            dim_out = num_heads * dim_head,
            length_scale = length_scale,
            length_scale_before = False,
            dtype = dtype,
            int8 = int8,
            init_mean = init_mean,
            init_std = init_std,
            bias = bias,
        )

        self.attention_out = Linear(
            dim_in = num_heads * dim_head,
            dim_out = dim_out,
            length_scale = length_scale,
            length_scale_before = False,
            dtype = dtype,
            int8 = int8,
            init_mean = init_mean,
            init_std = init_std,
            bias = bias,
        )
    
        self.dim_in = dim_in
        self.num_heads = num_heads
        self.dim_head = dim_head
        self.dim_out = dim_out
        self.int8 = int8
        self.length_scale = length_scale
        self.attn_scale = attn_scale
        self.mask_value = mask_value

        if dropout_p:
            self.attention_dropout = torch.nn.Dropout(dropout_p)
        else:
            self.attention_dropout = None

        self.pos_bias_type = pos_bias_type
        self.softmax = torch.nn.Softmax(dim=-1)

    def forward(self, 
            query : torch.Tensor,                   # (batch, len_q, dim_model)
            key_value : torch.Tensor,               # (batch, len_k, dim_model)
            mask : torch.Tensor,                    # (batch, len_q, len_k)
            position_bias : Optional[torch.Tensor] = None  # (num_heads, len_q, len_k) or (1, num_heads, len_q, len_k)
        ):
        """
        Args:
            query : (batch, len_q, dim_model)           fp16
            key_value : (batch, len_k, dim_model)       fp16
            mask : (batch, len_q, len_k)                fp16
            position_bias : (num_heads, len_q, len_k)   fp16
        Returns:
            out : (batch, len_q, dim_model)             fp16
        """

        batch_size = query.size(0)
        len_q = query.size(1)
        len_k = key_value.size(1)

        h_q = self.project_q(query)             # (batch, len_q, num_heads * dim_head)
        h_k = self.project_k(key_value)         # (batch, len_k, num_heads * dim_head)
        h_v = self.project_v(key_value)         # (batch, len_k, num_heads * dim_head)

        h_q = h_q.view(batch_size, len_q, self.num_heads, self.dim_head).permute(0, 2, 1, 3)   # (batch, num_heads, len_q, dim_head)
        h_k = h_k.view(batch_size, len_k, self.num_heads, self.dim_head).permute(0, 2, 1, 3)   # (batch, num_heads, len_k, dim_head)
        h_v = h_v.view(batch_size, len_k, self.num_heads, self.dim_head).permute(0, 2, 1, 3)   # (batch, num_heads, len_k, dim_head)

        h_q = h_q.contiguous().view(batch_size * self.num_heads, len_q, self.dim_head)      # (batch * num_heads, len_q, dim_head)
        h_k = h_k.contiguous().view(batch_size * self.num_heads, len_k, self.dim_head)      # (batch * num_heads, len_k, dim_head)
        h_v = h_v.contiguous().view(batch_size * self.num_heads, len_k, self.dim_head)      # (batch * num_heads, len_k, dim_head)

        if self.pos_bias_type == "rotary":
            h_q, h_k = position_bias(h_q, h_k)

        # (batch * num_heads, len_q, dim_head) @ (batch * num_heads, len_k, dim_head)T 
        # => (batch * num_heads, len_q, len_k)
        
        score = torch.matmul( h_q, h_k.transpose(1, 2))
        if self.attn_scale:
            score = score / math.sqrt(self.dim_head)

        # (batch, num_heads, len_q, len_k) 
        score = score.view(batch_size, self.num_heads, len_q, len_k)

        if self.pos_bias_type == "relative":
            if position_bias is not None:
                # (batch, num_heads, len_q, len_k) + (1, num_heads, len_q, len_k) 
                score = score + position_bias
        
        score = torch.where(
            mask.view(batch_size, 1, len_q, len_k),
            score.view(batch_size, self.num_heads, len_q, len_k),
            torch.scalar_tensor(self.mask_value, device=score.device, dtype=score.dtype)
        )   # (batch, num_heads, len_q, len_k)

        score = self.softmax(score)

        # avoid nan in softmax
        score = torch.where(
            mask.view(batch_size, 1, len_q, len_k),
            score.view(batch_size, self.num_heads, len_q, len_k),
            torch.scalar_tensor(0, device=score.device, dtype=score.dtype)
        ).view(batch_size * self.num_heads, len_q, len_k) # (batch * num_heads, len_q, len_k)

        if self.attention_dropout is not None:
            score = self.attention_dropout(score)

         # (batch * num_heads, len_q, len_k) @ (batch * num_heads, len_k, dim_head) = (batch * num_heads, len_q, dim_head)
        score = torch.matmul(score, h_v)

        score = score.view(batch_size, self.num_heads, len_q, self.dim_head).permute(0, 2, 1, 3) # (batch, len_q, num_heads, dim_head)
        score = score.view(batch_size, len_q, self.num_heads * self.dim_head) # (batch, len_q, num_heads * dim_head)

        # (1#batch, dim_model, num_heads * dim_head) @ (batch, num_heads * dim_head, len_q) = (batch, dim_model, len_q)
        score = self.attention_out(score)

        return score
