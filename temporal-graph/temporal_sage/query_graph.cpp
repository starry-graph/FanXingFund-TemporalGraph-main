#include <torch/extension.h>
#include <pybind11/pybind11.h>

#include <nebula/client/Config.h>
#include <nebula/client/ConnectionPool.h>
#include <common/datatypes/DataSet.h>

#include <sstream>
#include <memory>
#include <cstdint>
#include <iostream>
#include <unordered_set>

// #define CHECK_CPU(x) TORCH_CHECK(x.device().is_cpu(), #x " must be a CPU tensor")
// #define CHECK_CUDA(x) TORCH_CHECK(x.device().is_cuda(), #x " must be a CUDA tensor")
// #define CHECK_CONTIGUOUS(x) TORCH_CHECK(x.is_contiguous(), #x " must be contiguous")
// #define CHECK_INPUT(x) CHECK_CPU(x); CHECK_CONTIGUOUS(x)

struct LayerParam {
    std::string edge_type;
    std::int64_t limit;
    std::tuple<std::int64_t, std::int64_t> time_range;
    std::int8_t direction;
};

class QueryGraphChannel {
    std::unique_ptr<nebula::ConnectionPool> pool;
    bool verbose = false;
public:
    QueryGraphChannel(const std::vector<std::string>& addresses, std::uint32_t pool_size)
        : pool(new nebula::ConnectionPool())
    {
        this->pool->init(addresses, nebula::Config{.maxConnectionPoolSize_ = pool_size});
    };

    torch::Tensor sample_subgraph(
        const std::string& space_name,
        torch::Tensor start_nodes,
        const std::vector<LayerParam>& params
    ) {
        // TORCH_CHECK(start_nodes.is_contiguous(), "input tensor must be contiguous");

        auto s = start_nodes.data_ptr<std::int64_t>();
        auto n = start_nodes.numel();
        auto stmt = this->get_nebula_query(space_name, s, n, params);
        if (this->verbose) {
            std::cout << stmt << std::endl;
        }

        auto result = this->nebula_execute(stmt);
        auto& data = result.data;

        std::int64_t m = data->rowSize();
        auto edge_index = torch::zeros({4, m}).toType(at::ScalarType::Long);
        auto ptr = edge_index.data_ptr<std::int64_t>();
        
        size_t k = 0;
        for (auto row = data->begin(); row != data->end(); ++row, ++k) {
            
            auto src = row->values[0];
            auto dst = row->values[1];
            auto dist = row->values[2];
            auto timestamp = row->values[3];

            TORCH_CHECK(src.isInt(), "internel error: 1");
            TORCH_CHECK(dst.isInt(), "internel error: 1");
            TORCH_CHECK(timestamp.isInt(), "internel error: 1");

            ptr[0*m + k] = src.getInt();
            ptr[1*m + k] = dst.getInt();
            ptr[2*m + k] = dist.getInt();
            ptr[3*m + k] = timestamp.getInt();
        }

        return edge_index;
    }

    std::vector<torch::Tensor> gather_attributes(
        const std::string& space_name,
        torch::Tensor start_nodes,
        const std::vector<std::string>& attrs
    ) {
        const size_t bs = 512;

        std::int64_t n = start_nodes.numel();
        std::int64_t f = attrs.size();

        auto node_index = torch::zeros({n}).toType(at::ScalarType::Long);
        auto node_attrs = torch::zeros({n, f}).toType(at::ScalarType::Float);
        
        size_t k = 0;
        for (size_t t = 0; t < n; t += bs) {
            auto _s = start_nodes.data_ptr<std::int64_t>() + t;
            auto _n = std::min(n - t, bs);
            auto stmt = this->get_nebula_gather(space_name, _s, _n, attrs);

            if (this->verbose) {
                std::cout << stmt << std::endl;
            }

            auto result = this->nebula_execute(stmt);
            auto& data = result.data;

            auto index_ptr = node_index.data_ptr<std::int64_t>();
            auto attrs_ptr = node_attrs.data_ptr<float>();

            for (auto row = data->begin(); row != data->end(); ++row, ++k) {

                auto nid = row->values[0];
                TORCH_CHECK(nid.isInt(), "internel error: 1");
                index_ptr[k] = nid.getInt();

                for (std::int64_t i = 0; i < f; i++) {
                    auto x = row->values[i + 1];
                    TORCH_CHECK(x.isNumeric(), "internel error: 2");
                    attrs_ptr[k*f + i] = x.toFloat().getFloat();
                    // if (x.isNumeric()) {
                    //     attrs_ptr[k*f + i] = x.toFloat().getFloat();
                    // } else {
                    //     attrs_ptr[k*f + i] = -1.0f;
                    // }
                }
            }
        }

        node_index = node_index.index({torch::indexing::Slice(0, k)});
        node_attrs = node_attrs.index({torch::indexing::Slice(0, k)});

        return {node_index, node_attrs};
    }

    void debug() {
        this->verbose = true;
    }

private:
    nebula::ExecutionResponse nebula_execute(const std::string& stmt) {
        auto session = this->pool->getSession("root", "nebula");
        TORCH_CHECK(session.valid(), "invalid sessions inside query_graph");

        auto result = session.execute(stmt);
        TORCH_CHECK(
            result.errorCode == nebula::ErrorCode::SUCCEEDED,
            result.errorMsg->c_str()
        );

        session.release();
        return result;
    }
    std::string get_nebula_query(
        const std::string& space_name,
        const std::int64_t* start_nodes,
        const std::int64_t  numel_nodes,
        const std::vector<LayerParam>& params
    ) {
        std::stringstream ss;
        ss << "USE " << space_name << "; ";

        for (size_t k = 0; k < params.size(); k++) {
            auto p = params[k];

            if (k) {
                ss << "$v" << k << " = GO FROM $v" << (k - 1) << ".dst OVER " << p.edge_type;
            } else {
                ss << "$v" << k << " = GO FROM ";
                for (std::int64_t i = 0; i < numel_nodes; i++) {
                    if (i) ss << ",";
                    ss << start_nodes[i];
                }
                ss << " OVER " << p.edge_type;
            }

            switch (p.direction)
            {
            case 1:
                ss << " REVERSELY";
                break;
            case 2:
                ss << " BIDIRECT";
                break;
            default:
                break;
            }

            auto s = std::get<0>(p.time_range);
            auto t = std::get<1>(p.time_range);
            if (s < 0 && t >= 0) {
                ss << " WHERE properties(edge).time_stamp <= " << t;
            } else if ( s >= 0 && t < 0) {
                ss << " WHERE properties(edge).time_stamp >= " << s;
            } else if ( s >= 0 && t >= 0) {
                ss << " WHERE properties(edge).time_stamp >= " << s;
                ss << " AND properties(edge).time_stamp <= " << t;
            }

            ss << " YIELD DISTINCT id($^) as src, id($$) as dst, properties(edge).time_stamp as `timestamp`";

            if (p.limit >= 0) {
                ss << " SAMPLE [" << p.limit << "]";
            }
            ss << "; ";
        }

        for (size_t k = 0; k < params.size(); k++) {
            if (k) ss << " UNION YIELD";
            else ss << "YIELD";
            ss << " $v" << k << ".src as src, $v" << k << ".dst as dst, " << k << " as DIST, $v" << k << ".`timestamp` as `timestamp`";
        }
        ss << "; ";
        return ss.str();
    }

    std::string get_nebula_gather(
        const std::string& space_name,
        const std::int64_t* start_nodes,
        const std::int64_t  numel_nodes,
        const std::vector<std::string>& attrs
    ) {
        std::stringstream ss;
        ss << "USE " << space_name << "; ";
        ss << "FETCH PROP ON * ";

        std::unordered_set<std::int64_t> vis;
        for (std::int64_t i = 0; i < numel_nodes; i++) {
            auto u = start_nodes[i];
            if (vis.count(u)) {
                continue;
            }
            vis.insert(u);

            if (i) ss << ",";
            ss << u;
        }
        ss << " YIELD id(vertex) as nid";
        for (size_t i = 0; i < attrs.size(); i++) {
            ss << ", properties(vertex)." << attrs[i] << " as `" <<attrs[i] << "`";
        }
        ss << "; ";
        return ss.str();
    }
};

PYBIND11_MODULE(TORCH_EXTENSION_NAME, m) {
    py::class_<LayerParam>(m, "LayerParam", py::dynamic_attr())
        .def(py::init<const std::string&, std::int64_t, std::tuple<std::int64_t, std::int64_t>, std::int8_t>());
    py::class_<QueryGraphChannel>(m, "QueryGraphChannel", py::dynamic_attr())
        .def(py::init<const std::vector<std::string>&, std::uint32_t>())
        .def("sample_subgraph", &QueryGraphChannel::sample_subgraph, py::call_guard<py::gil_scoped_release>())
        .def("gather_attributes", &QueryGraphChannel::gather_attributes, py::call_guard<py::gil_scoped_release>())
        .def("debug", &QueryGraphChannel::debug, py::call_guard<py::gil_scoped_release>());
}