// lastbook-llm: runs one local language model for the Lastbook app. It talks only through its standard input and output,
// one JSON object per line, and never opens a socket, a port or a web address. It reads one file, the model it is given.
//
//   lastbook-llm --model FILE [--ctx N] [--threads N] [--batch N] [--ubatch N] [--gpu-reserve-mb N] [--cpu-only] [--measure]
//
// On start it fits the model to this computer's memory with llama.cpp's own fitting (common/fit.cpp, the same as llama-server's
// default --fit), prints {"ev":"fit",...} with what it measured, then loads the model and prints {"ev":"ready",...}.
// --measure stops after the fit line and loads nothing.
//
// Requests, one a line on standard input:
//   {"op":"gen","id":1,"segments":[["<|turn>",true],["text",false],...],"max_tokens":1800,"temp":0.2,"top_k":64,"top_p":0.95,
//    "min_p":0.05,"seed":1,"stop":["<|tool_response>"]}
//       A segment marked true is template markup and its special tokens are read as such. Text marked false never is: a document
//       holding "<|turn>" stays plain text and cannot start a turn of its own.
//   {"op":"cancel"}   stops the answer being written
//   {"op":"quit"}     or the end of standard input: leave
// Answers: {"ev":"tok","id":1,"t":"..."} for each piece, then {"ev":"done","id":1,"reason":"eog|stop|length|cancel",...}
// or {"ev":"error","id":1,"code":"...","msg":"..."}. Everything else, llama.cpp's own log included, goes to standard error.

#include "llama.h"
#include "ggml-backend.h"
#include "fit.h"
#include "log.h"

#include <nlohmann/json.hpp>

#include <atomic>
#include <chrono>
#include <climits>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <deque>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#  include <fcntl.h>
#  include <io.h>
#elif defined(__APPLE__)
#  include <unistd.h>
#  include <sys/sysctl.h>
#  include <mach/mach.h>
#else
#  include <unistd.h>
#  include <sys/sysinfo.h>
#endif

using json = nlohmann::ordered_json;

// log.cpp asks common.cpp whether the terminal takes colours. common.cpp is not built in (it brings the rest of llama.cpp's
// common library with it), and this program's output is never a terminal.
bool tty_can_use_colors() { return false; }
bool tty_enable_ansi() { return false; }

static FILE * g_out = nullptr;      // the protocol: the real standard output, kept apart from anything a library prints
static std::mutex g_out_lock;

static void send(const json & j) {
    std::string line = j.dump(-1, ' ', false, json::error_handler_t::replace) + "\n";
    std::lock_guard<std::mutex> lk(g_out_lock);
    fwrite(line.data(), 1, line.size(), g_out);
    fflush(g_out);
}

static void log_to_stderr(ggml_log_level level, const char * text, void *) {
    if (level >= GGML_LOG_LEVEL_INFO || level == GGML_LOG_LEVEL_CONT) {
        fputs(text, stderr);
    }
}

static uint64_t ram_total() {
#ifdef _WIN32
    MEMORYSTATUSEX m = { sizeof(m) };
    return GlobalMemoryStatusEx(&m) ? m.ullTotalPhys : 0;
#elif defined(__APPLE__)
    uint64_t v = 0;
    size_t n = sizeof(v);
    return sysctlbyname("hw.memsize", &v, &n, nullptr, 0) == 0 ? v : 0;
#else
    struct sysinfo si;
    return sysinfo(&si) == 0 ? (uint64_t) si.totalram * si.mem_unit : 0;
#endif
}

static uint64_t ram_free() {   // what could be had without pushing other programs out: free plus what the system would drop
#ifdef _WIN32
    MEMORYSTATUSEX m = { sizeof(m) };
    return GlobalMemoryStatusEx(&m) ? m.ullAvailPhys : 0;
#elif defined(__APPLE__)
    vm_statistics64_data_t s;
    mach_msg_type_number_t count = HOST_VM_INFO64_COUNT;
    if (host_statistics64(mach_host_self(), HOST_VM_INFO64, (host_info64_t) &s, &count) != KERN_SUCCESS) {
        return 0;
    }
    return (uint64_t) (s.free_count + s.inactive_count + s.purgeable_count) * (uint64_t) vm_page_size;
#else
    FILE * f = fopen("/proc/meminfo", "r");     // Linux's own estimate of the same: MemAvailable
    unsigned long long kb = 0;
    char line[256];
    while (f && fgets(line, sizeof(line), f)) {
        if (sscanf(line, "MemAvailable: %llu kB", &kb) == 1) {
            break;
        }
    }
    if (f) {
        fclose(f);
    }
    return (uint64_t) kb * 1024;
#endif
}

// ------------------------------------------------------------------------------------------------- requests from the app --
struct Inbox {
    std::mutex m;
    std::condition_variable cv;
    std::deque<json> q;
    std::atomic<bool> cancel{false};
    std::atomic<bool> quit{false};
};

static void read_requests(Inbox * in) {
    std::string line;
    while (std::getline(std::cin, line)) {
        if (line.empty()) {
            continue;
        }
        json j = json::parse(line, nullptr, false);
        if (j.is_discarded() || !j.is_object()) {
            send({{"ev", "error"}, {"code", "bad_request"}, {"msg", "not a JSON object"}});
            continue;
        }
        const std::string op = j.value("op", "");
        if (op == "cancel") {
            in->cancel = true;
            continue;
        }
        if (op == "quit") {
            break;
        }
        std::lock_guard<std::mutex> lk(in->m);
        in->q.push_back(std::move(j));
        in->cv.notify_one();
    }
    in->quit = true;      // the app closed the pipe or asked us to go: whatever is running stops too
    in->cancel = true;
    in->cv.notify_one();
}

// ------------------------------------------------------------------------------------------------------------ generation --
struct Engine {
    llama_model * model = nullptr;
    llama_context * ctx = nullptr;
    const llama_vocab * vocab = nullptr;
    std::vector<llama_token> cached;   // the tokens the context holds, in order
    Inbox * in = nullptr;
};

static std::vector<llama_token> tokenize(const llama_vocab * vocab, const std::string & text, bool special) {
    std::vector<llama_token> out(text.size() + 8);
    int n = llama_tokenize(vocab, text.data(), (int32_t) text.size(), out.data(), (int32_t) out.size(), false, special);
    if (n < 0) {
        out.resize(-n);
        n = llama_tokenize(vocab, text.data(), (int32_t) text.size(), out.data(), (int32_t) out.size(), false, special);
    }
    out.resize(n > 0 ? n : 0);
    return out;
}

static std::string piece(const llama_vocab * vocab, llama_token t) {
    char buf[256];
    int n = llama_token_to_piece(vocab, t, buf, sizeof(buf), 0, true);
    if (n < 0) {
        std::string big(-n, '\0');
        n = llama_token_to_piece(vocab, t, big.data(), (int32_t) big.size(), 0, true);
        return big.substr(0, n > 0 ? n : 0);
    }
    return std::string(buf, n);
}

static size_t complete_utf8(const std::string & s) {   // how much of s ends on a whole character; the rest waits for the next piece
    size_t n = s.size();
    for (size_t back = 1; back <= 4 && back <= n; back++) {
        unsigned char c = (unsigned char) s[n - back];
        if ((c & 0xC0) == 0x80) {
            continue;                      // a continuation byte: look further back for the lead
        }
        size_t need = c < 0x80 ? 1 : (c >> 5) == 6 ? 2 : (c >> 4) == 14 ? 3 : (c >> 3) == 30 ? 4 : 1;
        return back >= need ? n : n - back;
    }
    return n;
}

static bool decode_all(Engine & e, const std::vector<llama_token> & toks, size_t from, int & err) {
    const int n_batch = (int) llama_n_batch(e.ctx);
    for (size_t i = from; i < toks.size(); i += n_batch) {
        if (e.in->cancel) {
            err = 2;
            return false;
        }
        int n = (int) std::min<size_t>(n_batch, toks.size() - i);
        int rc = llama_decode(e.ctx, llama_batch_get_one(const_cast<llama_token *>(toks.data() + i), n));
        if (rc != 0) {
            err = rc == 2 ? 2 : 1;    // 2: aborted by our callback
            return false;
        }
        e.cached.insert(e.cached.end(), toks.begin() + i, toks.begin() + i + n);
    }
    return true;
}

static void generate(Engine & e, const json & req) {
    using clock = std::chrono::steady_clock;
    const auto t0 = clock::now();
    const json id = req.value("id", json());
    auto fail = [&](const char * code, const std::string & msg) {
        send({{"ev", "error"}, {"id", id}, {"code", code}, {"msg", msg}});
    };
    std::vector<llama_token> toks;
    if (llama_vocab_get_add_bos(e.vocab)) {
        toks.push_back(llama_vocab_bos(e.vocab));
    }
    for (const auto & seg : req.value("segments", json::array())) {
        if (!seg.is_array() || seg.size() != 2 || !seg[0].is_string() || !seg[1].is_boolean()) {
            return fail("bad_request", "segments are [text, special] pairs");
        }
        auto part = tokenize(e.vocab, seg[0].get<std::string>(), seg[1].get<bool>());
        toks.insert(toks.end(), part.begin(), part.end());
    }
    const int n_ctx = (int) llama_n_ctx(e.ctx);
    int max_tokens = std::max(1, req.value("max_tokens", 1024));
    if ((int) toks.size() + 16 >= n_ctx) {
        return fail("too_long", "the question and its sources are " + std::to_string(toks.size()) + " tokens; the model holds " + std::to_string(n_ctx));
    }
    max_tokens = std::min(max_tokens, n_ctx - (int) toks.size());

    // Reuse what the context already holds: a follow-up round repeats the conversation so far.
    size_t keep = 0;
    while (keep < e.cached.size() && keep < toks.size() && e.cached[keep] == toks[keep]) {
        keep++;
    }
    if (keep == toks.size()) {
        keep--;                            // the last prompt token is decoded again, for its logits
    }
    llama_memory_t mem = llama_get_memory(e.ctx);
    if (keep < e.cached.size()) {
        if (!llama_memory_seq_rm(mem, 0, (llama_pos) keep, -1)) {
            llama_memory_clear(mem, true);   // the sliding-window layers cannot always go back: start over
            keep = 0;
        }
        e.cached.resize(keep);
    }
    const size_t reused = keep;

    e.in->cancel = false;
    int err = 0;
    if (!decode_all(e, toks, keep, err)) {
        llama_memory_clear(mem, true);
        e.cached.clear();
        if (err == 2) {
            send({{"ev", "done"}, {"id", id}, {"reason", "cancel"}, {"n_prompt", toks.size()}, {"n_reused", reused}, {"n_gen", 0}});
            return;
        }
        return fail("decode", "the model could not read the question (llama_decode failed)");
    }
    const auto t1 = clock::now();

    llama_sampler * smpl = llama_sampler_chain_init(llama_sampler_chain_default_params());
    llama_sampler_chain_add(smpl, llama_sampler_init_top_k(req.value("top_k", 64)));
    llama_sampler_chain_add(smpl, llama_sampler_init_top_p(req.value("top_p", 0.95f), 1));
    llama_sampler_chain_add(smpl, llama_sampler_init_min_p(req.value("min_p", 0.05f), 1));
    llama_sampler_chain_add(smpl, llama_sampler_init_temp(req.value("temp", 0.2f)));
    llama_sampler_chain_add(smpl, llama_sampler_init_dist(req.value("seed", (uint32_t) LLAMA_DEFAULT_SEED)));

    std::vector<std::string> stops;
    for (const auto & s : req.value("stop", json::array())) {
        if (s.is_string()) {
            stops.push_back(s.get<std::string>());
        }
    }
    std::string pending, why = "length", stopped_by;
    int n_gen = 0;
    for (; n_gen < max_tokens; n_gen++) {
        if (e.in->cancel) {
            why = "cancel";
            break;
        }
        llama_token t = llama_sampler_sample(smpl, e.ctx, -1);
        if (llama_vocab_is_eog(e.vocab, t)) {
            why = "eog";
            break;
        }
        std::string p = piece(e.vocab, t);
        bool hit = false;
        for (const auto & s : stops) {
            if (p == s) {
                hit = true;
                stopped_by = s;
            }
        }
        if (hit) {
            why = "stop";
            break;
        }
        pending += p;
        size_t ok = complete_utf8(pending);
        if (ok) {
            send({{"ev", "tok"}, {"id", id}, {"t", pending.substr(0, ok)}});
            pending.erase(0, ok);
        }
        llama_token one = t;
        if (llama_decode(e.ctx, llama_batch_get_one(&one, 1)) != 0) {
            why = e.in->cancel ? "cancel" : "decode";
            break;
        }
        e.cached.push_back(t);
    }
    llama_sampler_free(smpl);
    if (!pending.empty()) {
        send({{"ev", "tok"}, {"id", id}, {"t", pending}});
    }
    if (why == "decode") {
        llama_memory_clear(mem, true);
        e.cached.clear();
        return fail("decode", "the model stopped while writing (llama_decode failed)");
    }
    const auto t2 = clock::now();
    auto ms = [](auto a, auto b) { return (long long) std::chrono::duration_cast<std::chrono::milliseconds>(b - a).count(); };
    json done = {{"ev", "done"}, {"id", id}, {"reason", why}, {"n_prompt", toks.size()}, {"n_reused", reused}, {"n_gen", n_gen},
                 {"prompt_ms", ms(t0, t1)}, {"gen_ms", ms(t1, t2)}};
    if (!stopped_by.empty()) {
        done["stop"] = stopped_by;
    }
    send(done);
}

// ------------------------------------------------------------------------------------------------------------------ start --
static json rows_json(const std::vector<common_memory_breakdown_row> & rows, uint64_t & device_bytes, uint64_t & host_bytes) {
    json out = json::array();
    device_bytes = host_bytes = 0;
    for (const auto & r : rows) {
        if (r.kind == common_memory_breakdown_row::BUFFER_TYPE) {
            continue;
        }
        const uint64_t used = r.model + r.context + r.compute;
        json j = {{"kind", r.kind == common_memory_breakdown_row::HOST ? "host" : "device"}, {"name", r.name}, {"desc", r.description},
                  {"model", r.model}, {"context", r.context}, {"compute", r.compute}};
        if (r.kind == common_memory_breakdown_row::DEVICE) {
            j["total"] = r.total;
            j["free"] = r.free;
            device_bytes += used;
        } else {
            host_bytes += used;
        }
        out.push_back(j);
    }
    return out;
}

static bool on_abort(void * data) {
    return ((Inbox *) data)->cancel.load();
}

static bool on_progress(float p, void * data) {
    static int last = -1;
    int pct = (int) (p * 100);
    if (pct / 5 != last / 5) {
        last = pct;
        send({{"ev", "progress"}, {"p", pct}});
    }
    return !((Inbox *) data)->quit.load();
}

int main(int argc, char ** argv) {
    // The protocol keeps the real standard output to itself. File descriptor 1 then points at standard error, so a library
    // that prints can never break a line of JSON.
#ifdef _WIN32
    int proto = _dup(1);
    _dup2(2, 1);
    _setmode(proto, _O_BINARY);
    _setmode(_fileno(stdin), _O_BINARY);
    g_out = _fdopen(proto, "wb");
#else
    int proto = dup(1);
    dup2(2, 1);
    g_out = fdopen(proto, "w");
#endif
    std::string path;
    int n_ctx = 16384, threads = 0, batch = 2048, ubatch = 512, gpu_reserve_mb = 1024;
    bool cpu_only = false, measure = false;
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        if (a == "--model" && i + 1 < argc) {
            path = argv[++i];
        } else if (a == "--ctx" && i + 1 < argc) {
            n_ctx = std::atoi(argv[++i]);
        } else if (a == "--threads" && i + 1 < argc) {
            threads = std::atoi(argv[++i]);
        } else if ((a == "--batch" || a == "--ubatch" || a == "--gpu-reserve-mb") && i + 1 < argc) {
            char * end = nullptr;
            long value = std::strtol(argv[++i], &end, 10);
            if (!*argv[i] || *end || value < 1 || value > INT_MAX) {
                send({{"ev", "error"}, {"code", "usage"}, {"msg", "expected a positive integer for " + a}});
                return 2;
            }
            if (a == "--batch") batch = (int) value;
            if (a == "--ubatch") ubatch = (int) value;
            if (a == "--gpu-reserve-mb") gpu_reserve_mb = (int) value;
        } else if (a == "--cpu-only") {
            cpu_only = true;
        } else if (a == "--measure") {
            measure = true;
        } else {
            send({{"ev", "error"}, {"code", "usage"}, {"msg", "unknown option " + a}});
            return 2;
        }
    }
    if (path.empty() || n_ctx < 32 || threads < 0 || ubatch > batch) {
        send({{"ev", "error"}, {"code", "usage"}, {"msg", "a model, ctx >= 32, threads >= 0 and ubatch <= batch are required"}});
        return 2;
    }
    llama_log_set(log_to_stderr, nullptr);
    common_log_set_verbosity_thold(LOG_LEVEL_WARN);
    ggml_backend_load_all();               // the backends beside this program, when they are separate files (Windows)
    llama_backend_init();

    Inbox in;
    llama_model_params mp = llama_model_default_params();
    llama_context_params cp = llama_context_default_params();
    cp.n_ctx = n_ctx;
    cp.n_batch = batch;
    cp.n_ubatch = ubatch;
    cp.n_seq_max = 1;
    if (threads > 0) {
        cp.n_threads = cp.n_threads_batch = threads;
    }
    cp.abort_callback = on_abort;
    cp.abort_callback_data = &in;
    cp.no_perf = true;
    cp.swa_full = false;   // as llama-server runs it: the sliding-window layers keep only their window, not the whole context
    static ggml_backend_dev_t no_devices[1] = { nullptr };
    if (cpu_only) {
        mp.devices = no_devices;
        mp.n_gpu_layers = 0;
    }

    // Fit: only settings left at llama.cpp's defaults are changed, and the context stays the size asked for.
    std::vector<float> split(llama_max_devices(), 0.0f);
    std::vector<llama_model_tensor_buft_override> overrides(llama_max_tensor_buft_overrides());
    std::vector<size_t> margins(llama_max_devices(), (size_t) gpu_reserve_mb * 1024 * 1024);
    auto t_fit = std::chrono::steady_clock::now();
    common_params_fit_status st = COMMON_PARAMS_FIT_STATUS_SUCCESS;
    if (!cpu_only) {
        st = common_fit_params(path.c_str(), &mp, &cp, split.data(), overrides.data(), margins.data(), (uint32_t) n_ctx, nullptr, GGML_LOG_LEVEL_ERROR);
    }
    if (st == COMMON_PARAMS_FIT_STATUS_ERROR) {
        send({{"ev", "error"}, {"code", "model_file"}, {"msg", "llama.cpp could not read the model file"}});
        return 3;
    }
    json fit = {{"ev", "fit"}, {"status", st == COMMON_PARAMS_FIT_STATUS_SUCCESS ? "fits" : "does_not_fit"}, {"n_ctx", n_ctx},
                {"n_gpu_layers", mp.n_gpu_layers}, {"cpu_only", cpu_only}, {"ram_total", ram_total()}, {"ram_free", ram_free()}};
    int n_over = 0;
    for (const auto & o : overrides) {
        if (!o.pattern) {
            break;
        }
        n_over++;
    }
    fit["host_overrides"] = n_over;
    try {
        uint32_t n_train = 0, n_got = 0;
        uint64_t dev = 0, host = 0;
        fit["memory"] = rows_json(common_fit_measure(path.c_str(), &mp, &cp, &n_train, &n_got, GGML_LOG_LEVEL_ERROR), dev, host);
        fit["device_bytes"] = dev;
        fit["host_bytes"] = host;
    } catch (const std::exception & ex) {
        fit["measure_error"] = ex.what();
    }
    fit["fit_ms"] = (long long) std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - t_fit).count();
    send(fit);
    if (measure) {
        return 0;
    }

    std::thread reader(read_requests, &in);
    reader.detach();
    mp.progress_callback = on_progress;
    mp.progress_callback_user_data = &in;
    auto t_load = std::chrono::steady_clock::now();
    Engine e;
    e.in = &in;
    e.model = llama_model_load_from_file(path.c_str(), mp);
    if (!e.model) {
        send({{"ev", "error"}, {"code", in.quit ? "quit" : "load"}, {"msg", "the model could not be loaded"}});
        return 4;
    }
    e.ctx = llama_init_from_model(e.model, cp);
    if (!e.ctx) {
        send({{"ev", "error"}, {"code", "context"}, {"msg", "there was not enough memory for the model's working space"}});
        llama_model_free(e.model);
        return 5;
    }
    e.vocab = llama_model_get_vocab(e.model);
    char desc[256] = {0};
    llama_model_desc(e.model, desc, sizeof(desc));
    send({{"ev", "ready"}, {"n_ctx", llama_n_ctx(e.ctx)}, {"desc", desc}, {"threads", llama_n_threads(e.ctx)},
          {"load_ms", (long long) std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - t_load).count()},
          {"ram_free", ram_free()}});

    while (true) {
        json req;
        {
            std::unique_lock<std::mutex> lk(in.m);
            in.cv.wait(lk, [&] { return in.quit || !in.q.empty(); });
            if (in.quit) {
                break;
            }
            req = std::move(in.q.front());
            in.q.pop_front();
        }
        if (req.value("op", "") == "gen") {
            try {
                generate(e, req);
            } catch (const std::exception & ex) {
                send({{"ev", "error"}, {"id", req.value("id", json())}, {"code", "bad_request"}, {"msg", ex.what()}});
            }
        } else {
            send({{"ev", "error"}, {"code", "bad_request"}, {"msg", "unknown op"}});
        }
    }
    llama_free(e.ctx);
    llama_model_free(e.model);
    llama_backend_free();
    return 0;
}
