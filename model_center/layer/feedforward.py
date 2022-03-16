import torch
import bmtrain as bmt

from .linear import Linear


@torch.jit.script
def gelu_impl(x):
    """OpenAI's gelu implementation."""
    return 0.5 * x * (1.0 + torch.tanh(0.7978845608028654 * x *
                                       (1.0 + 0.044715 * x * x)))

def gelu(x):
    return gelu_impl(x)


class DenseGatedACT(bmt.DistributedModule):

    def __init__(self,
                 dim_in : int,
                 dim_ff : int,
                 activate_fn : str = "gelu",
                 dtype = torch.half,
                 int8 = False,
                 init_mean = 0.0,
                 init_std = 0.02,
                 bias = False,
                 length_scale : bool = False,
        ):
        super().__init__()

        self.w_0 = Linear(
            dim_in = dim_in,
            dim_out = dim_ff,
            length_scale = length_scale,
            length_scale_before = False,
            dtype = dtype,
            int8 = int8,
            init_mean = init_mean,
            init_std = init_std,
            bias = bias,
        )

        self.w_1 = Linear(
            dim_in = dim_in,
            dim_out = dim_ff,
            length_scale = length_scale,
            length_scale_before = False,
            dtype = dtype,
            int8 = int8,
            init_mean = init_mean,
            init_std = init_std,
            bias = bias,
        )

        if activate_fn == "relu":
            self.act = torch.nn.ReLU()
        elif activate_fn == "gelu":
            self.act = gelu
        else:
            raise ValueError("Unsupported activation function: %s" % (activate_fn))
    
    def forward(self, x):
        """
        Args:
            x : (batch, seq_len, dim_in)
        Returns:
            x : (batch, seq_len, dim_ff)
        """

        gelu_score = self.act( self.w_0(x) )
        hidden_out = self.w_1(x)

        x = gelu_score * hidden_out
        return x


class DenseACT(bmt.DistributedModule):

    def __init__(self,
                 dim_in : int,
                 dim_ff : int,
                 activate_fn : str = "gelu",
                 dtype = torch.half,
                 int8 = False,
                 init_mean = 0.0,
                 init_std = 0.02,
                 bias = False,
                 length_scale : bool = False,
        ):
        super().__init__()

        self.w = Linear(
            dim_in = dim_in,
            dim_out = dim_ff,
            length_scale = length_scale,
            length_scale_before = False,
            dtype = dtype,
            int8 = int8,
            init_mean = init_mean,
            init_std = init_std,
            bias = bias,
        )
        
        if activate_fn == "relu":
            self.act = torch.nn.ReLU()
        elif activate_fn == "gelu":
            self.act = gelu
        else:
            raise ValueError("Unsupported activation function: %s" % (activate_fn))

    def forward(self, x):
        """
        Args:
            x : (batch, seq_len, dim_in)
        Returns:
            x : (batch, seq_len, dim_ff)
        """
        x = self.w(x)
        x = self.act(x)
        
        return x

class FeedForward(bmt.DistributedModule):

    def __init__(self,
                 dim_in : int, 
                 dim_ff : int,
                 dim_out = None,
                 dtype = torch.half, 
                 int8 = False,
                 init_mean = 0.0, 
                 init_std = 0.02,
                 bias = False,
                 activate_fn = "gated_gelu",
                 length_scale : bool = False,
                 dropout_p = 0,
        ):

        super().__init__()

        if activate_fn.startswith("gated_"):
            self.w_in = DenseGatedACT(
                dim_in = dim_in,
                dim_ff = dim_ff,
                activate_fn = activate_fn[6:],
                dtype = dtype,
                int8 = int8,
                init_mean = init_mean,
                init_std = init_std,
                bias = bias,
                length_scale = length_scale,
            )
        else:
            self.w_in = DenseACT(
                dim_in = dim_in,
                dim_ff = dim_ff,
                activate_fn = activate_fn,
                dtype = dtype,
                int8 = int8,
                init_mean = init_mean,
                init_std = init_std,
                bias = bias,
                length_scale = length_scale,
            )

        if dropout_p:
            self.dropout = torch.nn.Dropout(dropout_p)
        else:
            self.dropout = None

        if dim_out is None:
            dim_out = dim_in

        self.dim_ff = dim_ff
        self.dim_out = dim_out

        self.w_out = Linear(
            dim_in = dim_ff,
            dim_out = dim_out,
            length_scale = length_scale,
            length_scale_before = True,
            dtype = dtype,
            int8 = int8,
            init_mean = init_mean,
            init_std = init_std,
            bias = bias,
        )

        self.int8 = int8
        self.length_scale = length_scale

    def forward(self, x):
        """
        Args:
            x : (batch, seq_len, dim_in)       fp16
        Returns:
            out : (batch, seq_len, dim_out)     fp16
        """

        x = self.w_in(x)

        if self.dropout is not None:
            x = self.dropout(x)

        x = self.w_out(x)

        return x
