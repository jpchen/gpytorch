import torch
import math


class StochasticLQ(object):
    """
    Implements an approximate log determinant calculation for symmetric positive definite matrices
    using stochastic Lanczos quadrature. For efficient calculation of derivatives, We additionally
    compute the trace of the inverse using the same probe vector the log determinant was computed
    with. For more details, see Dong et al. 2017 (in submission).
    """
    def __init__(self, cls=None, max_iter=15, num_random_probes=10):
        """
        The nature of stochastic Lanczos quadrature is that the calculation of tr(f(A)) is both inaccurate and
        stochastic. An instance of StochasticLQ has two parameters that control these tradeoffs. Increasing either
        parameter increases the running time of the algorithm.
        Args:
            cls - Tensor constructor - to ensure correct type (default - default tensor)
            max_iter (scalar) - The number of Lanczos iterations to perform. Increasing this makes the estimate of
                     tr(f(A)) more accurate in expectation -- that is, the average value returned has lower error.
            num_random_probes (scalar) - The number of random probes to use in the stochastic trace estimation.
                              Increasing this makes the estimate of tr(f(A)) lower variance -- that is, the value
                              returned is more consistent.
        """
        self.cls = cls or torch.Tensor
        self.max_iter = max_iter
        self.num_random_probes = num_random_probes

    def lanczos_batch(self, matmul_closure, rhs_vectors):
        dim, num_vectors = rhs_vectors.size()
        num_iters = min(self.max_iter, dim)

        Q = self.cls(num_vectors, dim, num_iters).zero_()
        alpha = self.cls(num_vectors, num_iters).zero_()
        beta = self.cls(num_vectors, num_iters).zero_()

        rhs_vectors = rhs_vectors / torch.norm(rhs_vectors, 2, dim=0)
        Q[:, :, 0] = rhs_vectors
        U = rhs_vectors

        R = matmul_closure(U)
        a = U.mul(R).sum(0)

        rhs_vectors = (R - a * U) + 1e-10

        beta[:, 0] = torch.norm(rhs_vectors, 2, dim=0)
        alpha[:, 0] = a

        for k in range(1, num_iters):
            U, rhs_vectors, alpha_k, beta_k = self._lanczos_step_batch(U, rhs_vectors, matmul_closure, Q[:, :, :k])

            alpha[:, k] = alpha_k
            beta[:, k] = beta_k
            Q[:, :, k] = U

            if all(torch.abs(beta[:, k]) < 1e-4) or all(torch.abs(alpha[:, k]) < 1e-4):
                break

        if k == 1:
            Ts = alpha[:, :k].unsqueeze(1)
            Qs = Q[:, :, :k]
        else:
            alpha = alpha[:, :k]
            beta = beta[:, 1:k]

            Qs = Q[:, :, :k]

            Ts = self.cls(num_vectors, num_iters - 1, num_iters - 1)
            for i in range(num_vectors):
                Ts[i, :, :] = torch.diag(alpha[i, :]) + torch.diag(beta[i, :], 1) + torch.diag(beta[i, :], -1)

        return Qs, Ts

    def _lanczos_step_batch(self, U, rhs_vectors, matmul_closure, Q):
        num_vectors, dim, num_iters = Q.size()
        norm_vs = torch.norm(rhs_vectors, 2, dim=0)
        orig_U = U

        U = rhs_vectors / norm_vs

        U = U - self._batch_mv(Q, self._batch_mv(Q.transpose(1, 2), U.t()))

        U = U / torch.norm(U, 2, dim=0)

        R = matmul_closure(U) - norm_vs * orig_U

        a = U.mul(R).sum(0)
        rhs_vectors = (R - a * U) + 1e-10

        return U, rhs_vectors, a, norm_vs

    def _batch_mv(self, M, V):
        num, n, m = M.size()
        V_expand = V.expand(n, num, m).transpose(0, 1)

        return (M * V_expand).sum(2)

    def binary_search_symeig(self, T):
        left = 0
        right = len(T)
        while right - left > 1:
            mid = (left + right) // 2
            eigs = T[:mid, :mid].symeig()[0]
            if torch.min(eigs) < -1e-4:
                right = mid - 1
            else:
                left = mid

        return left

    def evaluate(self, matmul_closure, n, funcs):
        if torch.is_tensor(matmul_closure):
            lhs = matmul_closure
            if lhs.numel() == 1:
                return math.fabs(lhs.squeeze()[0])

            def default_matmul_closure(tensor):
                return lhs.matmul(tensor)
            matmul_closure = default_matmul_closure

        V = self.cls(n, self.num_random_probes).bernoulli_().mul_(2).add_(-1)
        V.div_(torch.norm(V, 2, 0).expand_as(V))

        results = [0] * len(funcs)

        _, Ts = self.lanczos_batch(matmul_closure, V)

        for j in range(self.num_random_probes):
            T = Ts[j, :, :]

            [f, Y] = T.symeig(eigenvectors=True)
            if min(f) < -1e-4:
                last_proper = max(self.binary_search_symeig(T), 1)
                [f, Y] = T[:last_proper, :last_proper].symeig(eigenvectors=True)

            for i, func in enumerate(funcs):
                results[i] = results[i] + n / float(self.num_random_probes) * (Y[0, :].pow(2).dot(func(f + 1.1e-4)))

        return results